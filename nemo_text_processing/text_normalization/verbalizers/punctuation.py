# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
# Copyright 2015 and onwards Google, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from nemo_text_processing.text_normalization.graph_utils import NEMO_CHAR, NEMO_NOT_QUOTE, GraphFst, delete_space

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class PunctuationFst(GraphFst):
    """
    Finite state transducer for classifying punctuation
        e.g. tokens { name: "a" } tokens { name: "," pause_length: "PAUSE_MEDIUM phrase_break: true type: PUNCT" }
            -> a ,
    """

    def __init__(self):
        super().__init__(name="punctuation", kind="verbalize")
        char = (
            pynutil.delete("name:")
            + delete_space
            + pynutil.delete("\"")
            + pynini.closure(NEMO_NOT_QUOTE, 1)
            + pynutil.delete("\" pause_length")
            + pynutil.delete(pynini.closure(NEMO_CHAR - "}"))
        )
        self.fst = char.optimize()
