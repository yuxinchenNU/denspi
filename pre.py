import collections
import json
import logging

import six
from tqdm import tqdm

import tokenization
from post import _improve_answer_span, _check_is_max_context

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


class SquadExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self,
                 qas_id=None,
                 question_text=None,
                 doc_tokens=None,
                 orig_answer_text=None,
                 start_position=None,
                 end_position=None,
                 title="",
                 doc_idx=0,
                 pid=0):
        self.qas_id = qas_id
        self.question_text = question_text
        self.doc_tokens = doc_tokens
        self.orig_answer_text = orig_answer_text
        self.start_position = start_position
        self.end_position = end_position
        self.title = title
        self.doc_idx = doc_idx
        self.pid = pid

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        s = ""
        s += "qas_id: %s" % (tokenization.printable_text(self.qas_id))
        s += ", question_text: %s" % (
            tokenization.printable_text(self.question_text))
        s += ", doc_tokens: [%s]" % (" ".join(self.doc_tokens))
        if self.start_position:
            s += ", start_position: %d" % (self.start_position)
        if self.start_position:
            s += ", end_position: %d" % (self.end_position)
        return s


class ContextFeatures(object):
    """A single set of features of data."""

    def __init__(self,
                 unique_id,
                 example_index,
                 doc_span_index,
                 tokens,
                 token_to_orig_map,
                 token_is_max_context,
                 input_ids,
                 input_mask,
                 start_position=None,
                 end_position=None):
        self.unique_id = unique_id
        self.example_index = example_index
        self.doc_span_index = doc_span_index
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map
        self.token_is_max_context = token_is_max_context
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.start_position = start_position
        self.end_position = end_position


class QuestionFeatures(object):
    def __init__(self,
                 unique_id,
                 example_index,
                 input_ids,
                 input_mask):
        self.unique_id = unique_id
        self.example_index = example_index
        self.input_ids = input_ids
        self.input_mask = input_mask


def read_squad_examples(input_file, is_training, context_only=False, question_only=False,
                        draft=False, draft_num_examples=12):
    """Read a SQuAD json file into a list of SquadExample."""
    with open(input_file, "r") as reader:
        input_data = json.load(reader)["data"]

    examples = []
    for doc_idx, entry in enumerate(input_data):
        title = entry['title']
        for pid, paragraph in enumerate(entry["paragraphs"]):
            if not question_only:
                paragraph_text = paragraph["context"]
                doc_tokens, char_to_word_offset = context_to_tokens_and_offset(paragraph_text)
            if context_only:
                example = SquadExample(
                    doc_tokens=doc_tokens,
                    title=title,
                    doc_idx=doc_idx,
                    pid=pid)
                examples.append(example)
                if draft and len(examples) == draft_num_examples:
                    return examples
                continue
            else:
                for qa in paragraph["qas"]:
                    qas_id = qa["id"]
                    question_text = qa["question"]
                    start_position = None
                    end_position = None
                    orig_answer_text = None
                    if is_training:
                        if False:  # len(qa["answers"]) > 1:
                            raise ValueError(
                                "For training, each question should have exactly 1 answer.")
                        elif len(qa["answers"]) == 0:
                            orig_answer_text = ""
                            start_position = -1
                            end_position = -1
                        else:
                            answer = qa["answers"][0]
                            orig_answer_text = answer["text"]
                            answer_offset = answer["answer_start"]
                            answer_length = len(orig_answer_text)
                            start_position = char_to_word_offset[answer_offset]
                            end_position = char_to_word_offset[answer_offset + answer_length - 1]
                            # Only add answers where the text can be exactly recovered from the
                            # document. If this CAN'T happen it's likely due to weird Unicode
                            # stuff so we will just skip the example.
                            #
                            # Note that this means for training mode, every example is NOT
                            # guaranteed to be preserved.
                            actual_text = " ".join(doc_tokens[start_position:(end_position + 1)])
                            cleaned_answer_text = " ".join(
                                tokenization.whitespace_tokenize(orig_answer_text))
                            if actual_text.find(cleaned_answer_text) == -1:
                                logger.warning("Could not find answer: '%s' vs. '%s'",
                                               actual_text, cleaned_answer_text)
                                continue

                    if question_only:
                        example = SquadExample(
                            qas_id=qas_id,
                            question_text=question_text)
                    else:
                        example = SquadExample(
                            qas_id=qas_id,
                            question_text=question_text,
                            doc_tokens=doc_tokens,
                            orig_answer_text=orig_answer_text,
                            start_position=start_position,
                            end_position=end_position,
                            title=title,
                            pid=pid)
                    examples.append(example)

                    if draft and len(examples) == draft_num_examples:
                        return examples
    return examples


# This is for training and direct evaluation (slow eval)
def convert_examples_to_features(examples, tokenizer, max_seq_length,
                                 doc_stride, max_query_length, is_training):
    """Loads a data file into a list of `InputBatch`s."""

    unique_id = 1000000000

    features = []
    question_features = []
    for (example_index, example) in enumerate(tqdm(examples, desc='converting')):

        query_tokens = tokenizer.tokenize(example.question_text)
        if len(query_tokens) > max_query_length:
            query_tokens = query_tokens[0:max_query_length]

        tok_to_orig_index = []
        orig_to_tok_index = []
        all_doc_tokens = []
        for (i, token) in enumerate(example.doc_tokens):
            orig_to_tok_index.append(len(all_doc_tokens))
            sub_tokens = tokenizer.tokenize(token)
            for sub_token in sub_tokens:
                tok_to_orig_index.append(i)
                all_doc_tokens.append(sub_token)

        tok_start_position = None
        tok_end_position = None
        if is_training:
            tok_start_position = orig_to_tok_index[example.start_position]
            if example.end_position < len(example.doc_tokens) - 1:
                tok_end_position = orig_to_tok_index[example.end_position + 1] - 1
            else:
                tok_end_position = len(all_doc_tokens) - 1
            (tok_start_position, tok_end_position) = _improve_answer_span(
                all_doc_tokens, tok_start_position, tok_end_position, tokenizer,
                example.orig_answer_text)

        # The -3 accounts for [CLS], [SEP] and [SEP]
        max_tokens_for_doc = max_seq_length - 2

        # We can have documents that are longer than the maximum sequence length.
        # To deal with this we do a sliding window approach, where we take chunks
        # of the up to our max length with a stride of `doc_stride`.
        _DocSpan = collections.namedtuple(  # pylint: disable=invalid-name
            "DocSpan", ["start", "length"])
        doc_spans = []
        start_offset = 0
        while start_offset < len(all_doc_tokens):
            length = len(all_doc_tokens) - start_offset
            if length > max_tokens_for_doc:
                length = max_tokens_for_doc
            doc_spans.append(_DocSpan(start=start_offset, length=length))
            if start_offset + length == len(all_doc_tokens):
                break
            start_offset += min(length, doc_stride)

        for (doc_span_index, doc_span) in enumerate(doc_spans):
            tokens = []
            tokens_ = []
            token_to_orig_map = {}
            token_is_max_context = {}
            tokens.append("[CLS]")
            tokens_.append("[CLS]")
            for token in query_tokens:
                tokens_.append(token)
            tokens_.append("[SEP]")

            for i in range(doc_span.length):
                split_token_index = doc_span.start + i
                token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]

                is_max_context = _check_is_max_context(doc_spans, doc_span_index,
                                                       split_token_index)
                token_is_max_context[len(tokens)] = is_max_context
                tokens.append(all_doc_tokens[split_token_index])
            tokens.append("[SEP]")

            input_ids = tokenizer.convert_tokens_to_ids(tokens)
            input_ids_ = tokenizer.convert_tokens_to_ids(tokens_)

            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1] * len(input_ids)
            input_mask_ = [1] * len(input_ids_)

            # Zero-pad up to the sequence length.
            while len(input_ids) < max_seq_length:
                input_ids.append(0)
                input_mask.append(0)

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length

            while len(input_ids_) < max_query_length + 2:
                input_ids_.append(0)
                input_mask_.append(0)

            assert len(input_ids_) == max_query_length + 2
            assert len(input_mask_) == max_query_length + 2

            start_position = None
            end_position = None
            if example.start_position is not None and example.start_position < 0:
                start_position, end_position = -1, -1
            elif is_training:
                # For training, if our document chunk does not contain an annotation
                # we throw it out, since there is nothing to predict.
                doc_start = doc_span.start
                doc_end = doc_span.start + doc_span.length - 1
                if (example.start_position < doc_start or
                    example.end_position < doc_start or
                    example.start_position > doc_end or example.end_position > doc_end):
                    continue

                doc_offset = 1
                start_position = tok_start_position - doc_start + doc_offset
                end_position = tok_end_position - doc_start + doc_offset

            if example_index < 20:
                logger.info("*** Example ***")
                logger.info("unique_id: %s" % (unique_id))
                logger.info("example_index: %s" % (example_index))
                logger.info("doc_span_index: %s" % (doc_span_index))
                logger.info("tokens: %s" % " ".join(
                    [tokenization.printable_text(x) for x in tokens]))
                logger.info("token_to_orig_map: %s" % " ".join(
                    ["%d:%d" % (x, y) for (x, y) in six.iteritems(token_to_orig_map)]))
                logger.info("token_is_max_context: %s" % " ".join([
                    "%d:%s" % (x, y) for (x, y) in six.iteritems(token_is_max_context)
                ]))
                logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
                logger.info(
                    "input_mask: %s" % " ".join([str(x) for x in input_mask]))
                if is_training:
                    answer_text = " ".join(tokens[start_position:(end_position + 1)])
                    logger.info("start_position: %d" % (start_position))
                    logger.info("end_position: %d" % (end_position))
                    logger.info(
                        "answer: %s" % (tokenization.printable_text(answer_text)))

            features.append(
                ContextFeatures(
                    unique_id=unique_id,
                    example_index=example_index,
                    doc_span_index=doc_span_index,
                    tokens=tokens,
                    token_to_orig_map=token_to_orig_map,
                    token_is_max_context=token_is_max_context,
                    input_ids=input_ids,
                    input_mask=input_mask,
                    start_position=start_position,
                    end_position=end_position))
            question_features.append(
                QuestionFeatures(
                    unique_id=unique_id,
                    example_index=example_index,
                    input_ids=input_ids_,
                    input_mask=input_mask_))
            unique_id += 1

    return features, question_features


# This is for embedding questions
def convert_questions_to_features(examples, tokenizer, max_query_length=None):
    """Loads a data file into a list of `InputBatch`s."""

    unique_id = 1000000000

    question_features = []
    for (example_index, example) in enumerate(tqdm(examples, desc='converting')):

        query_tokens = tokenizer.tokenize(example.question_text)
        if max_query_length is None:
            max_query_length = len(query_tokens)
        if len(query_tokens) > max_query_length:
            query_tokens = query_tokens[0:max_query_length]

        for _ in enumerate(range(1)):
            tokens_ = []
            tokens_.append("[CLS]")
            for token in query_tokens:
                tokens_.append(token)
            tokens_.append("[SEP]")

            input_ids_ = tokenizer.convert_tokens_to_ids(tokens_)

            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask_ = [1] * len(input_ids_)

            # Zero-pad up to the sequence length.
            while len(input_ids_) < max_query_length + 2:
                input_ids_.append(0)
                input_mask_.append(0)

            assert len(input_ids_) == max_query_length + 2
            assert len(input_mask_) == max_query_length + 2

            if example_index < 20:
                logger.info("*** Example ***")
                logger.info("unique_id: %s" % (unique_id))
                logger.info("example_index: %s" % (example_index))
                logger.info("tokens: %s" % " ".join(
                    [tokenization.printable_text(x) for x in query_tokens]))
                logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids_]))
                logger.info(
                    "input_mask: %s" % " ".join([str(x) for x in input_mask_]))

            question_features.append(
                QuestionFeatures(
                    unique_id=unique_id,
                    example_index=example_index,
                    input_ids=input_ids_,
                    input_mask=input_mask_))
            unique_id += 1

    return question_features


def convert_documents_to_features(examples, tokenizer, max_seq_length, doc_stride):
    """Loads a data file into a list of `InputBatch`s."""

    unique_id = 1000000000

    features = []
    for (example_index, example) in enumerate(tqdm(examples, desc='converting')):

        tok_to_orig_index = []
        orig_to_tok_index = []
        all_doc_tokens = []
        for (i, token) in enumerate(example.doc_tokens):
            orig_to_tok_index.append(len(all_doc_tokens))
            sub_tokens = tokenizer.tokenize(token)
            for sub_token in sub_tokens:
                tok_to_orig_index.append(i)
                all_doc_tokens.append(sub_token)

        # The -3 accounts for [CLS], [SEP] and [SEP]
        max_tokens_for_doc = max_seq_length - 2

        # We can have documents that are longer than the maximum sequence length.
        # To deal with this we do a sliding window approach, where we take chunks
        # of the up to our max length with a stride of `doc_stride`.
        _DocSpan = collections.namedtuple(  # pylint: disable=invalid-name
            "DocSpan", ["start", "length"])
        doc_spans = []
        start_offset = 0
        while start_offset < len(all_doc_tokens):
            length = len(all_doc_tokens) - start_offset
            if length > max_tokens_for_doc:
                length = max_tokens_for_doc
            doc_spans.append(_DocSpan(start=start_offset, length=length))
            if start_offset + length == len(all_doc_tokens):
                break
            start_offset += min(length, doc_stride)

        for (doc_span_index, doc_span) in enumerate(doc_spans):
            tokens = []
            token_to_orig_map = {}
            token_is_max_context = {}
            tokens.append("[CLS]")

            for i in range(doc_span.length):
                split_token_index = doc_span.start + i
                token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]

                is_max_context = _check_is_max_context(doc_spans, doc_span_index,
                                                       split_token_index)
                token_is_max_context[len(tokens)] = is_max_context
                tokens.append(all_doc_tokens[split_token_index])
            tokens.append("[SEP]")

            input_ids = tokenizer.convert_tokens_to_ids(tokens)

            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1] * len(input_ids)

            # Zero-pad up to the sequence length.
            while len(input_ids) < max_seq_length:
                input_ids.append(0)
                input_mask.append(0)

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length

            if example_index < 20:
                logger.info("*** Example ***")
                logger.info("unique_id: %s" % (unique_id))
                logger.info("example_index: %s" % (example_index))
                logger.info("doc_span_index: %s" % (doc_span_index))
                logger.info("tokens: %s" % " ".join(
                    [tokenization.printable_text(x) for x in tokens]))
                logger.info("token_to_orig_map: %s" % " ".join(
                    ["%d:%d" % (x, y) for (x, y) in six.iteritems(token_to_orig_map)]))
                logger.info("token_is_max_context: %s" % " ".join([
                    "%d:%s" % (x, y) for (x, y) in six.iteritems(token_is_max_context)
                ]))
                logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
                logger.info(
                    "input_mask: %s" % " ".join([str(x) for x in input_mask]))

            features.append(
                ContextFeatures(
                    unique_id=unique_id,
                    example_index=example_index,
                    doc_span_index=doc_span_index,
                    tokens=tokens,
                    token_to_orig_map=token_to_orig_map,
                    token_is_max_context=token_is_max_context,
                    input_ids=input_ids,
                    input_mask=input_mask))
            unique_id += 1

    return features


def context_to_tokens_and_offset(context):
    def is_whitespace(c):
        if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
            return True
        return False

    doc_tokens = []
    char_to_word_offset = []
    prev_is_whitespace = True
    for c in context:
        if is_whitespace(c):
            prev_is_whitespace = True
        else:
            if prev_is_whitespace:
                doc_tokens.append(c)
            else:
                doc_tokens[-1] += c
            prev_is_whitespace = False
        char_to_word_offset.append(len(doc_tokens) - 1)

    return doc_tokens, char_to_word_offset
