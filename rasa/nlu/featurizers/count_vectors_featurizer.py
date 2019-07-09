import logging
import os
import re
from typing import Any, Dict, List, Optional, Text

import numpy as np
from rasa.nlu import utils
from rasa.nlu.config import RasaNLUModelConfig
from rasa.nlu.featurizers import Featurizer
from rasa.nlu.model import Metadata
from rasa.nlu.training_data import Message, TrainingData

logger = logging.getLogger(__name__)


class CountVectorsFeaturizer(Featurizer):
    """Bag of words featurizer

    Creates bag-of-words representation of intent features
    using sklearn's `CountVectorizer`.
    All tokens which consist only of digits (e.g. 123 and 99
    but not ab12d) will be represented by a single feature.

    Set `analyzer` to 'char_wb'
    to use the idea of Subword Semantic Hashing
    from https://arxiv.org/abs/1810.07150.
    """

    provides = ["text_features"]

    requires = []

    defaults = {
        # the parameters are taken from
        # sklearn's CountVectorizer
        "sequence": False,

        "use_shared_vocab": False,
        "sparse": False,

        # whether to use word or character n-grams
        # 'char_wb' creates character n-grams inside word boundaries
        # n-grams at the edges of words are padded with space.
        "analyzer": "word",  # use 'char' or 'char_wb' for character
        # regular expression for tokens
        # only used if analyzer == 'word'
        "token_pattern": r"(?u)\b\w\w+\b",
        # remove accents during the preprocessing step
        "strip_accents": None,  # {'ascii', 'unicode', None}
        # list of stop words
        "stop_words": None,  # string {'english'}, list, or None (default)
        # min document frequency of a word to add to vocabulary
        # float - the parameter represents a proportion of documents
        # integer - absolute counts
        "min_df": 1,  # float in range [0.0, 1.0] or int
        # max document frequency of a word to add to vocabulary
        # float - the parameter represents a proportion of documents
        # integer - absolute counts
        "max_df": 1.0,  # float in range [0.0, 1.0] or int
        # set range of ngrams to be extracted
        "min_ngram": 1,  # int
        "max_ngram": 1,  # int
        # limit vocabulary size
        "max_features": None,  # int or None
        # if convert all characters to lowercase
        "lowercase": True,  # bool
        # handling Out-Of-Vacabulary (OOV) words
        # will be converted to lowercase if lowercase is True
        "OOV_token": None,  # string or None
        "OOV_words": [],  # string or list of strings
    }

    @classmethod
    def required_packages(cls) -> List[Text]:
        return ["sklearn"]

    def _load_count_vect_params(self):
        self.sequence = self.component_config['sequence']
        self.use_shared_vocab = self.component_config['use_shared_vocab']
        self.sparse = self.component_config['sparse']
        # set analyzer
        self.analyzer = self.component_config["analyzer"]

        # regular expression for tokens
        self.token_pattern = self.component_config["token_pattern"]

        # remove accents during the preprocessing step
        self.strip_accents = self.component_config["strip_accents"]

        # list of stop words
        self.stop_words = self.component_config["stop_words"]

        # min number of word occurancies in the document to add to vocabulary
        self.min_df = self.component_config["min_df"]

        # max number (fraction if float) of word occurancies
        # in the document to add to vocabulary
        self.max_df = self.component_config["max_df"]

        # set ngram range
        self.min_ngram = self.component_config["min_ngram"]
        self.max_ngram = self.component_config["max_ngram"]

        # limit vocabulary size
        self.max_features = self.component_config["max_features"]

        # if convert all characters to lowercase
        self.lowercase = self.component_config["lowercase"]

        # Flag to check if test data has been featurized or not.
        self.is_test_data_featurized = False

    # noinspection PyPep8Naming
    def _load_OOV_params(self):
        self.OOV_token = self.component_config["OOV_token"]

        self.OOV_words = self.component_config["OOV_words"]
        if self.OOV_words and not self.OOV_token:
            logger.error(
                "The list OOV_words={} was given, but "
                "OOV_token was not. OOV words are ignored."
                "".format(self.OOV_words)
            )
            self.OOV_words = []

        if self.lowercase and self.OOV_token:
            # convert to lowercase
            self.OOV_token = self.OOV_token.lower()
            if self.OOV_words:
                self.OOV_words = [w.lower() for w in self.OOV_words]

    def _check_analyzer(self):
        if self.analyzer != "word":
            if self.OOV_token is not None:
                logger.warning(
                    "Analyzer is set to character, "
                    "provided OOV word token will be ignored."
                )
            if self.stop_words is not None:
                logger.warning(
                    "Analyzer is set to character, "
                    "provided stop words will be ignored."
                )
            if self.max_ngram == 1:
                logger.warning(
                    "Analyzer is set to character, "
                    "but max n-gram is set to 1. "
                    "It means that the vocabulary will "
                    "contain single letters only."
                )

    def __init__(self, component_config=None):
        """Construct a new count vectorizer using the sklearn framework."""

        super(CountVectorsFeaturizer, self).__init__(component_config)

        # parameters for sklearn's CountVectorizer
        self._load_count_vect_params()

        # handling Out-Of-Vacabulary (OOV) words
        self._load_OOV_params()

        # warn that some of config parameters might be ignored
        self._check_analyzer()

        # declare class instance for CountVectorizer
        self.vect = None

    def _tokenizer(self, text):
        """Override tokenizer in CountVectorizer."""

        text = re.sub(r"\b[0-9]+\b", "__NUMBER__", text)

        token_pattern = re.compile(self.token_pattern)
        tokens = token_pattern.findall(text)

        if self.OOV_token:
            if hasattr(self.vect, "vocabulary_"):
                # CountVectorizer is trained, process for prediction
                if self.OOV_token in self.vect.vocabulary_:
                    tokens = [
                        t if t in self.vect.vocabulary_.keys() else self.OOV_token
                        for t in tokens
                    ]
            elif self.OOV_words:
                # CountVectorizer is not trained, process for train
                tokens = [self.OOV_token if t in self.OOV_words else t for t in tokens]

        return tokens

    @staticmethod
    def _get_message_text(message):
        if message.get("spacy_doc"):  # if lemmatize is possible
            return " ".join([t.lemma_ for t in message.get("spacy_doc")])
        elif message.get("tokens"):  # if directly tokens is provided
            return " ".join([t.text for t in message.get("tokens")])
        else:
            return message.text

    @staticmethod
    def _get_message_intent(message):
        return ' '.join([t.text for t in message.get("intent_tokens")])

    @staticmethod
    def _get_message_response(message):
        if message.get("response_tokens") is None:
            return ' '
        return ' '.join([t.text for t in message.get("response_tokens")])

    @staticmethod
    def _get_text_sequence(text):
        return text.split()

    # noinspection PyPep8Naming
    def _check_OOV_present(self, examples):
        if self.OOV_token and not self.OOV_words:
            for t in examples:
                if self.OOV_token in t or (
                    self.lowercase and self.OOV_token in t.lower()
                ):
                    return

            logger.warning(
                "OOV_token='{}' was given, but it is not present "
                "in the training data. All unseen words "
                "will be ignored during prediction."
                "".format(self.OOV_token)
            )

    def _create_sequence(self, vect, texts):
        feature_len = len(vect.vocabulary_.keys())

        texts = [self._get_text_sequence(text) for text in texts]

        if self.sparse:
            X = []
        else:
            seq_len = max([len(tokens) for tokens in texts])
            num_exs = len(texts)
            X = np.ones([num_exs, seq_len, feature_len], dtype=np.int32) * -1

        for i, tokens in enumerate(texts):
            x = vect.transform(tokens)
            x.sort_indices()
            if self.sparse:
                X.append(x)
            else:
                X[i, :x.shape[0], :] = x.toarray()
        return X

    def train(
        self, training_data: TrainingData, cfg: RasaNLUModelConfig = None, **kwargs: Any
    ) -> None:

        """Train the featurizer.

        Take parameters from config and
        construct a new count vectorizer using the sklearn framework.
        """

        from sklearn.feature_extraction.text import CountVectorizer

        spacy_nlp = kwargs.get("spacy_nlp")
        if spacy_nlp is not None:
            # create spacy lemma_ for OOV_words
            self.OOV_words = [t.lemma_
                              for w in self.OOV_words
                              for t in spacy_nlp(w)]
        if self.use_shared_vocab:
            self.vect = CountVectorizer(token_pattern=self.token_pattern,
                                        strip_accents=self.strip_accents,
                                        lowercase=self.lowercase,
                                        stop_words=self.stop_words,
                                        ngram_range=(self.min_ngram,
                                                     self.max_ngram),
                                        max_df=self.max_df,
                                        min_df=self.min_df,
                                        max_features=self.max_features,
                                        tokenizer=self._tokenizer,
                                        analyzer=self.analyzer)
        else:
            self.vect = [CountVectorizer(token_pattern=self.token_pattern,
                                         strip_accents=self.strip_accents,
                                         lowercase=self.lowercase,
                                         stop_words=self.stop_words,
                                         ngram_range=(self.min_ngram,
                                                      self.max_ngram),
                                         max_df=self.max_df,
                                         min_df=self.min_df,
                                         max_features=self.max_features,
                                         tokenizer=self._tokenizer,
                                         analyzer=self.analyzer),
                         CountVectorizer(token_pattern=self.token_pattern,
                                         strip_accents=self.strip_accents,
                                         lowercase=self.lowercase,
                                         stop_words=self.stop_words,
                                         ngram_range=(self.min_ngram,
                                                      self.max_ngram),
                                         max_df=self.max_df,
                                         min_df=self.min_df,
                                         max_features=self.max_features,
                                         tokenizer=self._tokenizer,
                                         analyzer=self.analyzer)]

        lem_exs = [
            self._get_message_text(example) for example in training_data.intent_examples
        ]

        self._check_OOV_present(lem_exs)

        lem_ints = [self._get_message_intent(example)
                    for example in training_data.intent_examples]

        lem_resps = [self._get_message_response(example)
                     for example in training_data.intent_examples]

        self._check_OOV_present(lem_ints)
        self._check_OOV_present(lem_resps)

        # noinspection PyPep8Naming
        try:
            if not self.sequence:
                if self.use_shared_vocab:
                    self.vect.fit(lem_exs + lem_ints + lem_resps)
                    X = self.vect.transform(lem_exs)
                    Y_ints = self.vect.transform(lem_ints)
                    Y_resps = self.vect.transform(lem_resps)
                else:
                    X = self.vect[0].fit_transform(lem_exs)
                    Y_ints = self.vect[1].fit_transform(lem_ints)
                    Y_resps = self.vect[1].fit_transform(lem_resps)

                if not self.sparse:
                    X = X.toarray()
                    Y_ints = Y_ints.toarray()
                    Y_resps = Y_resps.toarray()
                else:
                    X.sort_indices()
                    Y_ints.sort_indices()
                    Y_resps.sort_indices()

            else:
                if self.use_shared_vocab:
                    self.vect.fit(lem_exs + lem_ints)
                    X = self._create_sequence(self.vect, lem_exs)
                    Y_ints = self._create_sequence(self.vect, lem_ints)
                    Y_resps = self._create_sequence(self.vect, lem_resps)
                else:
                    self.vect[0].fit(lem_exs)
                    X = self._create_sequence(self.vect[0], lem_exs)
                    self.vect[1].fit(lem_ints + lem_resps)
                    Y_ints = self._create_sequence(self.vect[1], lem_ints)
                    Y_resps = self._create_sequence(self.vect[1], lem_resps)

        except ValueError:
            self.vect = None
            return

        for i, example in enumerate(training_data.intent_examples):
            # create bag for each example

            if not self.sequence and not self.sparse:
                example.set(
                    "text_features",
                    self._combine_with_existing_text_features(example, X[i]),
                )
            else:
                example.set("text_features", X[i])

            example.set("intent_features", Y_ints[i])
            example.set("response_features", Y_resps[i])

    def process(self, message: Message, **kwargs: Any) -> None:
        if self.vect is None:
            logger.error(
                "There is no trained CountVectorizer: "
                "component is either not trained or "
                "didn't receive enough training data"
            )
        else:

            featurized_test_data = None

            if "test_data" in kwargs and not self.is_test_data_featurized:

                logger.info("Adding BOW features to intents of test set for the first time")

                test_data = kwargs["test_data"]

                lem_ints = [self._get_message_intent(example)
                                for example in test_data.intent_examples]

                lem_resps = [self._get_message_response(example) for example in test_data.intent_examples]

                lem_comb = lem_ints + lem_resps

                self._check_OOV_present(lem_comb)

                if self.use_shared_vocab:
                    vect = self.vect
                else:
                    vect = self.vect[1]

                if not self.sequence:
                    Y_ints = vect.transform(lem_ints)
                    Y_resps = vect.transform(lem_resps)
                    if not self.sparse:
                        Y_ints = Y_ints.toarray()
                        Y_resps = Y_resps.toarray()
                else:
                    Y_ints = self._create_sequence(vect, lem_ints)
                    Y_resps = self._create_sequence(vect, lem_resps)

                for i, example in enumerate(test_data.intent_examples):
                    example.set("intent_features", Y_ints[i])
                    example.set("response_features", Y_resps[i])

                featurized_test_data = test_data

                self.is_test_data_featurized = True

            message_text = self._get_message_text(message)
            if self.use_shared_vocab:
                vect = self.vect
            else:
                vect = self.vect[0]

            if not self.sparse:
                if not self.sequence:
                    bag = vect.transform([message_text]).toarray().squeeze()
                    message.set("text_features",
                                self._combine_with_existing_text_features(message,
                                                                          bag))
                else:
                    seq = self._create_sequence(vect, [message_text]).squeeze()
                    message.set("text_features", seq)
            else:
                if not self.sequence:
                    bag = vect.transform([message_text])
                    bag.sort_indices()
                    message.set("text_features", bag)
                else:
                    seq = self._create_sequence(vect, [message_text])
                    message.set("text_features", seq)

            return featurized_test_data

    def persist(self, file_name: Text, model_dir: Text) -> Optional[Dict[Text, Any]]:
        """Persist this model into the passed directory.

        Returns the metadata necessary to load the model again.
        """

        file_name = file_name + ".pkl"
        featurizer_file = os.path.join(model_dir, file_name)
        utils.pycloud_pickle(featurizer_file, self)
        return {"file": file_name}

    @classmethod
    def load(
        cls,
        meta: Dict[Text, Any],
        model_dir: Text = None,
        model_metadata: Metadata = None,
        cached_component: Optional["CountVectorsFeaturizer"] = None,
        **kwargs: Any
    ) -> "CountVectorsFeaturizer":

        if model_dir and meta.get("file"):
            file_name = meta.get("file")
            featurizer_file = os.path.join(model_dir, file_name)
            return utils.pycloud_unpickle(featurizer_file)
        else:
            logger.warning(
                "Failed to load featurizer. Maybe path {} "
                "doesn't exist".format(os.path.abspath(model_dir))
            )
            return CountVectorsFeaturizer(meta)