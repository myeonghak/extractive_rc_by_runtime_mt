import json
import logging
from typing import Dict, List, Tuple

from overrides import overrides
import json
import logging
from typing import Dict, List, Tuple, Any
from collections import Counter, defaultdict
import string

from allennlp.common.file_utils import cached_path
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.instance import Instance
from allennlp.data.dataset_readers.reading_comprehension import util
from allennlp.data.token_indexers import SingleIdTokenIndexer, TokenIndexer
from allennlp.data.tokenizers import Token, Tokenizer, WordTokenizer
from allennlp.data.fields import Field, TextField, IndexField, MetadataField

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@DatasetReader.register("squadeval")
class SquadReaderEval(DatasetReader):
    """
    Reads a JSON-formatted SQuAD file and returns a ``Dataset`` where the ``Instances`` have four
    fields: ``question``, a ``TextField``, ``passage``, another ``TextField``, and ``span_start``
    and ``span_end``, both ``IndexFields`` into the ``passage`` ``TextField``.  We also add a
    ``MetadataField`` that stores the instance's ID, the original passage text, gold answer strings,
    and token offsets into the original passage, accessible as ``metadata['id']``,
    ``metadata['original_passage']``, ``metadata['answer_texts']`` and
    ``metadata['token_offsets']``.  This is so that we can more easily use the official SQuAD
    evaluation script to get metrics.

    Parameters
    ----------
    tokenizer : ``Tokenizer``, optional (default=``WordTokenizer()``)
        We use this ``Tokenizer`` for both the question and the passage.  See :class:`Tokenizer`.
        Default is ```WordTokenizer()``.
    token_indexers : ``Dict[str, TokenIndexer]``, optional
        We similarly use this for both the question and the passage.  See :class:`TokenIndexer`.
        Default is ``{"tokens": SingleIdTokenIndexer()}``.
    """

    def __init__(self,
                 tokenizer=None,
                 token_indexers=None,
                 lazy=False):
        super().__init__(lazy)
        self._tokenizer = tokenizer or WordTokenizer()
        self._token_indexers = token_indexers or {
            'tokens': SingleIdTokenIndexer()}

    @overrides
    def _read(self, file_path):
        # if `file_path` is a URL, redirect to the cache
        file_path = cached_path(file_path)

        logger.info("Reading file at %s", file_path)
        with open(file_path) as dataset_file:
            dataset_json = json.load(dataset_file)
            dataset = dataset_json['data']
        logger.info("Reading the dataset")
        for article in dataset:
            for paragraph_json in article['paragraphs']:
                paragraph = paragraph_json["context"]
                tokenized_paragraph = self._tokenizer.tokenize(paragraph)

                for question_answer in paragraph_json['qas']:
                    question_text = question_answer["question"].strip(
                    ).replace("\n", "")
                    answer_texts = [answer['text']
                                    for answer in question_answer['answers']]
                    span_starts = [answer['answer_start']
                                   for answer in question_answer['answers']]
                    span_ends = [
                        start + len(answer) for start, answer in zip(span_starts, answer_texts)]
                    question_id = question_answer['id']
                    instance = self.text_to_instance(question_text,
                                                     paragraph,
                                                     zip(span_starts,
                                                         span_ends),
                                                     answer_texts,
                                                     tokenized_paragraph,
                                                     question_id)
                    yield instance

    @overrides
    def text_to_instance(self,  # type: ignore
                         question_text,
                         passage_text,
                         char_spans=None,
                         answer_texts=None,
                         passage_tokens=None,\
                         question_id=None):
        # pylint: disable=arguments-differ
        if not passage_tokens:
            passage_tokens = self._tokenizer.tokenize(passage_text)
        char_spans = char_spans or []

        # We need to convert character indices in `passage_text` to token indices in
        # `passage_tokens`, as the latter is what we'll actually use for supervision.
        token_spans: List[Tuple[int, int]] = []
        passage_offsets = [(token.idx, token.idx + len(token.text))
                           for token in passage_tokens]
        for char_span_start, char_span_end in char_spans:
            (span_start, span_end), error = util.char_span_to_token_span(passage_offsets,
                                                                         (char_span_start, char_span_end))
            if error:
                logger.debug("Passage: %s", passage_text)
                logger.debug("Passage tokens: %s", passage_tokens)
                logger.debug("Question text: %s", question_text)
                logger.debug("Answer span: (%d, %d)",
                             char_span_start, char_span_end)
                logger.debug("Token span: (%d, %d)", span_start, span_end)
                logger.debug("Tokens in answer: %s",
                             passage_tokens[span_start:span_end + 1])
                logger.debug(
                    "Answer: %s", passage_text[char_span_start:char_span_end])
            token_spans.append((span_start, span_end))

        return make_reading_comprehension_instance(self._tokenizer.tokenize(question_text),
                                                   passage_tokens,
                                                   self._token_indexers,
                                                   passage_text,
                                                   token_spans,
                                                   answer_texts,
                                                   question_id)

    @classmethod
    def from_params(cls, params):
        # print(params.as_ordered_dict())
        dataset_type = params.pop("type")
        tokenizer = Tokenizer.from_params(params.pop('tokenizer', {}))
        token_indexers = TokenIndexer.dict_from_params(
            params.pop('token_indexers', {}))
        lazy = params.pop('lazy', False)
        params.assert_empty(cls.__name__)
        return cls(tokenizer=tokenizer, token_indexers=token_indexers, lazy=lazy)


def make_reading_comprehension_instance(question_tokens,
                                        passage_tokens,
                                        token_indexers,
                                        passage_text,
                                        token_spans=None,
                                        answer_texts=None,
                                        question_id=None,
                                        additional_metadata=None):
    fields = {}
    additional_metadata = additional_metadata or {}
    passage_offsets = [(token.idx, token.idx + len(token.text))
                       for token in passage_tokens]

    # This is separate so we can reference it later with a known type.
    passage_field = TextField(passage_tokens, token_indexers)
    fields['passage'] = passage_field
    fields['question'] = TextField(question_tokens, token_indexers)
    metadata = {
        'original_passage': passage_text,
        'token_offsets': passage_offsets,
        'question_tokens': [token.text for token in question_tokens],
        'passage_tokens': [token.text for token in passage_tokens],
    }

    if answer_texts:
        metadata['answer_texts'] = answer_texts
    if question_id:
        metadata['question_id'] = question_id
    else:
        metadata['question_id'] = '0'

    if token_spans:
        # There may be multiple answer annotations, so we pick the one that occurs the most.  This
        # only matters on the SQuAD dev set, and it means our computed metrics ("start_acc",
        # "end_acc", and "span_acc") aren't quite the same as the official metrics, which look at
        # all of the annotations.  This is why we have a separate official SQuAD metric calculation
        # (the "em" and "f1" metrics use the official script).
        candidate_answers: Counter = Counter()
        for span_start, span_end in token_spans:
            candidate_answers[(span_start, span_end)] += 1
        span_start, span_end = candidate_answers.most_common(1)[0][0]

        fields['span_start'] = IndexField(span_start, passage_field)
        fields['span_end'] = IndexField(span_end, passage_field)
        metadata.update(additional_metadata)
        fields['metadata'] = MetadataField(metadata)

    return Instance(fields)
