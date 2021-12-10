import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple, Union

import torch

from nemo.collections.nlp.models.machine_translation import MTEncDecModel
from nemo.collections.nlp.modules.common.transformer import BeamSearchSequenceGenerator


def get_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="The script is for restoring punctuation and capitalization in text. Long strings are split into "
        "segments of length `--max_seq_length`. `--max_seq_length` is the length which includes [CLS] and [SEP] "
        "tokens. Parameter `--step` controls segments overlapping. `--step` is a distance between beginnings of "
        "consequent segments. Model outputs for tokens near the borders of tensors are less accurate and can be "
        "discarded before final predictions computation. Parameter `--margin` is number of discarded outputs near "
        "segments borders. If model predictions in overlapping parts of segments are different most frequent "
        "predictions is chosen.",
    )
    input_ = parser.add_mutually_exclusive_group(required=True)
    input_.add_argument(
        "--input_manifest",
        "-m",
        type=Path,
        help="Path to the file with NeMo manifest which needs punctuation and capitalization. If the first element "
        "of manifest contains key 'pred_text', 'pred_text' values are passed for tokenization. Otherwise 'text' "
        "values are passed for punctuation and capitalization. Exactly one parameter of `--input_manifest` and "
        "`--input_text` should be provided.",
    )
    input_.add_argument(
        "--input_text",
        "-t",
        type=Path,
        help="Path to file with text which needs punctuation and capitalization. Exactly one parameter of "
        "`--input_manifest` and `--input_text` should be provided.",
    )
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument(
        "--output_manifest",
        "-M",
        type=Path,
        help="Path to output NeMo manifest. Text with restored punctuation and capitalization will be saved in "
        "'pred_text' elements if 'pred_text' key is present in the input manifest. Otherwise text with restored "
        "punctuation and capitalization will be saved in 'text' elements. Exactly one parameter of `--output_manifest` "
        "and `--output_text` should be provided.",
    )
    output.add_argument(
        "--output_text",
        "-T",
        type=Path,
        help="Path to file with text with restored punctuation and capitalization. Exactly one parameter of "
        "`--output_manifest` and `--output_text` should be provided.",
    )
    parser.add_argument(
        "--model_path",
        "-P",
        type=Path,
        help=f"Path to .nemo checkpoint of `MTEncDecModel`. No more than one of parameters ",
        required=True,
    )
    parser.add_argument(
        "--max_seq_length",
        "-L",
        type=int,
        default=64,
        help="Numbers of words in segments into which queries are split.",
    )
    parser.add_argument(
        "--step",
        "-s",
        type=int,
        default=8,
        help="Number of words between beginnings of consequent segments."
    )
    parser.add_argument(
        "--margin",
        "-g",
        type=int,
        default=16,
        help="A number of words near borders in segments which are not used for punctuation and capitalization "
        "prediction.",
    )
    parser.add_argument(
        "--batch_size", "-b", type=int, default=128, help="Number of segments which are processed simultaneously.",
    )
    parser.add_argument(
        "--beam_size", type=int, default=4, help="Number of rays for beam search."
    )
    parser.add_argument(
        "--len_pen", type=float, default=0.6, help="Length penalty for beam search."
    )
    parser.add_argument(
        "--max_delta_length",
        type=int,
        default=512,
        help="Maximum difference of lengths of source and target sequences for beam search",
    )
    parser.add_argument(
        "--device",
        "-d",
        choices=['cpu', 'cuda'],
        help="Which device to use. If device is not set and CUDA is available, then GPU will be used. If device is "
        "not set and CUDA is not available, then CPU is used.",
    )
    parser.add_argument(
        "--add_source_num_words_to_batch",
        action="store_true",
        help="Whether to pass number of words in source sequences to beam search generator. Set this if fixed length " \
        "beam search is used."
    )
    parser.add_argument(
        "--capitalization_labels",
        default="OuU",
        help="A string containing all characters used as capitalization labels. THE FIRST CHARACTER IN A STRING HAS "
        "TO BE NEUTRAL LABEL."
    )
    parser.add_argument(
        "--no_all_upper_label",
        action="store_true",
        help="Whether to use 'u' as first character capitalization and 'U' as capitalization of all characters in a "
        "word. If not set, then 'U' is for capitalization of first character in a word, 'O' for absence of "
        "capitalization, 'u' is not used.",
    )
    parser.add_argument(
        "--save_labels_instead_of_text",
        "-B",
        action="store_true",
        help="If this option is set, then punctuation and capitalization labels are saved instead text with restored "
        "punctuation and capitalization. Labels are saved in autoregressive format.",
    )
    args = parser.parse_args()
    if args.input_manifest is None and args.output_manifest is not None:
        parser.error("--output_manifest requires --input_manifest")
    if args.max_seq_length <= 0:
        parser.error(
            f"Parameter `--max_seq_length` has to be positive, whereas `--max_seq_length={args.max_seq_length}`"
        )
    if args.max_seq_length - 2 * args.margin < args.step:
        parser.error(
            f"Parameters `--max_seq_length`, `--margin`, `--step` must satisfy condition "
            f"`max_seq_length - 2 * margin >= step` whereas `--max_seq_length={args.max_seq_length}`, "
            f"`--margin={args.margin}`, `--step={args.step}`."
        )
    for name in ["input_manifest", "input_text", "output_manifest", "output_text", "model_path"]:
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).expanduser())
    return args


def load_manifest(manifest: Path) -> List[Dict[str, Union[str, float]]]:
    result = []
    with manifest.open() as f:
        for i, line in enumerate(f):
            data = json.loads(line)
            result.append(data)
    return result


def split_into_segments(texts: List[str], max_seq_length: int, step: int) -> Tuple[List[str], List[int], List[int]]:
    segments, query_indices, start_word_i = [], [], []
    segment_start = 0
    for q_i, query in enumerate(texts):
        words = query.split()
        while segment_start + max_seq_length < len(words):
            segments.append(' '.join(words[segment_start : segment_start + max_seq_length]))
            start_word_i.append(segment_start)
            query_indices.append(q_i)
            segment_start += step
    return segments, query_indices, start_word_i


def adjust_predicted_labels_length(
    segments: List[str], autoregressive_labels: List[str], capitalization_labels: str
) -> List[str]:
    result = []
    capitalization_pattern = re.compile(f"[{capitalization_labels}]")
    for i, (segment, labels) in enumerate(zip(segments, autoregressive_labels)):
        num_words = len(segment.split())
        num_word_labels = len(capitalization_pattern.findall(segment))
        if num_words > num_word_labels:
            if labels[-1] != ' ':
                labels += ' ' + (capitalization_labels[0] + ' ') * (num_words - num_word_labels)
        elif num_words < num_word_labels:
            i = num_word_labels
            pos = len(labels) - 1
            while i > num_words:
                if labels[pos] in capitalization_labels:
                    i -= 1
                pos -= 1
            labels = labels[: pos + 1]
        result.append(labels)
    return result


def update_label_counter(
    counter: Dict[str, List[int]], lbl: str, num_words_in_segment: int, word_ind_in_segment: int
) -> None:
    if lbl in counter:
        counter[lbl][0] += 1
        counter[lbl][1] += min(word_ind_in_segment + 1, num_words_in_segment - word_ind_in_segment - 1)
    else:
        counter[lbl] = [1, min(word_ind_in_segment + 1, num_words_in_segment - word_ind_in_segment - 1)]


def get_label_votes(
    query: str,
    q_i: int,
    current_segment_i: int,
    segment_autoregressive_labels: List[str],
    query_indices: List[int],
    step: int,
    margin: int,
    capitalization_labels: str,
) -> Tuple[List[Dict[str, List[int]]], List[Dict[str, List[int]]], int]:
    capitalization_pattern = re.compile(f"([{capitalization_labels}])")
    words = query.split()
    num_words = len(words)
    punctuation_voting = [{} for _ in range(num_words + 1)]
    capitalization_voting = [{} for _ in range(num_words)]
    segment_id_in_query = 0
    while query_indices[current_segment_i] == q_i:
        num_words_in_segment = len(capitalization_pattern.findall(segment_autoregressive_labels[current_segment_i]))
        the_last_segment = segment_id_in_query * step + num_words_in_segment >= num_words
        labels = capitalization_labels.split(segment_autoregressive_labels[current_segment_i])
        num_processed_words_in_segment = 0
        for lbl_i, lbl in enumerate(labels):
            if lbl in capitalization_labels:
                num_processed_words_in_segment += 1
            if segment_id_in_query > 0 and num_processed_words_in_segment <= margin != 0:
                continue
            if not the_last_segment and num_processed_words_in_segment > num_words_in_segment - margin:
                break
            query_word_i = step * current_segment_i + num_processed_words_in_segment - 1
            if lbl_i % 2:
                assert lbl in capitalization_labels
                update_label_counter(
                    capitalization_voting[query_word_i], lbl, num_words_in_segment, num_processed_words_in_segment - 1
                )
            else:
                assert lbl not in capitalization_labels
                update_label_counter(
                    punctuation_voting[query_word_i], lbl, num_words_in_segment, num_processed_words_in_segment - 1
                )
        segment_id_in_query += 1
        current_segment_i += 1
    return punctuation_voting, capitalization_voting, current_segment_i


def select_best_label(votes):
    votes = sorted(votes.items(), key=lambda x: -x[1][1] / x[1][0])
    votes = sorted(votes, key=lambda x: -x[1][0])
    return votes[0][0]


def apply_autoregressive_labels(
    queries: List[str],
    segment_autoregressive_labels: List[str],
    query_indices: List[int],
    start_word_i: List[int],
    step: int,
    margin: int,
    capitalization_labels: str,
    no_all_upper_label: bool,
) -> Tuple[List[str], List[str]]:
    processed_queries = []
    united_labels = []
    current_segment_i = 0
    for q_i, query in enumerate(queries):
        punctuation_voting, capitalization_voting, current_segment_i = get_label_votes(
            query,
            q_i,
            current_segment_i,
            segment_autoregressive_labels,
            query_indices,
            step,
            margin,
            capitalization_labels,
        )
        words = query.split()
        # Leading punctuation
        processed_query = select_best_label(punctuation_voting[0])
        united = processed_query
        for i, (word, cv, pv) in enumerate(zip(words, capitalization_voting, punctuation_voting)):
            capitalization_label = select_best_label(cv)
            punctuation_label = select_best_label(cv)
            error_msg = f"Unexpected capitalization label {repr(capitalization_label)} in word {i} in a query {q_i}."
            if no_all_upper_label:
                if capitalization_label == 'U':
                    processed_query += word.capitalize()
                elif capitalization_label == 'O':
                    processed_query += word
                else:
                    raise ValueError(error_msg)
            else:
                if capitalization_label == 'U':
                    processed_query += word.upper()
                elif capitalization_label == 'u':
                    processed_query += word.capitalize()
                elif capitalization_label == 'O':
                    processed_query += word
                else:
                    raise ValueError(error_msg)
            processed_query += punctuation_label
            united += capitalization_label + punctuation_label
        processed_queries.append(processed_query)
        united_labels.append(united)
    return processed_queries, united_labels


def main():
    args = get_args()
    model = MTEncDecModel.restore_from(args.model_path)
    if args.device is None:
        if torch.cuda.is_available():
            model = model.cuda()
        else:
            model = model.cpu()
    else:
        model = model.to(args.device)
    if args.input_manifest is None:
        texts = []
        with args.input_text.open() as f:
            for line in f:
                texts.append(line.strip())
    else:
        manifest = load_manifest(args.input_manifest)
        text_key = "pred_text" if "pred_text" in manifest[0] else "text"
        texts = []
        for item in manifest:
            texts.append(item[text_key])
    not_empty_queries, not_empty_indices = [], []
    empty_queries, empty_indices = [], []
    for i, text in enumerate(texts):
        if text.strip():
            not_empty_queries.append(text)
            not_empty_indices.append(i)
        else:
            empty_queries.append(text)
            empty_indices.append(i)
    segments, query_indices, start_word_i = split_into_segments(not_empty_queries, args.max_seq_length, args.margin)
    model.beam_search = BeamSearchSequenceGenerator(
        embedding=model.decoder.embedding,
        decoder=model.decoder.decoder,
        log_softmax=model.log_softmax,
        bos=model.decoder_tokenizer.bos_id,
        pad=model.decoder_tokenizer.pad_id,
        eos=model.decoder_tokenizer.eos_id,
        max_sequence_length=model.decoder.max_sequence_length,
        beam_size=args.beam_size,
        len_pen=args.len_pen,
        max_delta_length=args.max_delta_length,
        decoder_word_ids=model.decoder_tokenizer.word_ids,
    )
    autoregressive_punctuation_labels = []
    for i in range(0, len(segments), args.batch_size):
        autoregressive_punctuation_labels += model.translate(
            text=segments[i : i + args.batch_size],
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            return_beam_scores=args.write_scores,
            log_timing=args.write_timing,
            add_src_num_words_to_batch=args.add_src_num_words_to_batch,
        )
    processed_queries, united_labels = apply_autoregressive_labels(
        texts,
        autoregressive_punctuation_labels,
        query_indices,
        start_word_i,
        args.step,
        args.margin,
        args.capitalization_labels,
        args.no_all_upper_label,
    )
    result_texts = [""] * len(texts)
    result_labels = [""] * len(texts)
    for i, processed_query, labels in zip(not_empty_indices, processed_queries, united_labels):
        result_texts[i] = processed_query
        result_labels[i] = labels
    for i, empty_query in zip(empty_indices, empty_queries):
        result_texts[i] = empty_query
        result_labels[i] = empty_query
    if args.output_manifest is None:
        args.output_text.parent.mkdir(exist_ok=True, parents=True)
        with args.output_text.open('w') as f:
            for t in (result_labels if args.save_labels_instead_of_text else result_texts):
                f.write(t + '\n')
    else:
        args.output_manifest.parent.mkdir(exist_ok=True, parents=True)
        with args.output_manifest.open('w') as f:
            for item, t in zip(manifest, result_labels if args.save_labels_instead_of_text else result_texts):
                item[text_key] = t
                f.write(json.dumps(item) + '\n')


if __name__ == "__main__":
    main()