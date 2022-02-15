# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

import copy
import itertools
import subprocess
import json
import math
import os
import pickle as pkl
import numpy as np
from typing import Dict, List, Optional, Union

import librosa
import torch
from statistics import mode
from omegaconf import DictConfig
from omegaconf.omegaconf import open_dict
from pytorch_lightning import Trainer
from torch.utils.data import ChainDataset
from collections import OrderedDict
from nemo.collections.asr.models import ClusteringDiarizer
from nemo.collections.asr.data.audio_to_label import AudioToSpeechLabelDataset, AudioToSpeechTSVADDataset
from nemo.collections.asr.data.audio_to_label_dataset import get_tarred_speech_label_dataset
from nemo.collections.asr.data.audio_to_text_dataset import convert_to_config_list
from nemo.collections.asr.losses.angularloss import AngularSoftmaxLoss
from nemo.collections.asr.losses.bce_loss import BCELoss
from nemo.collections.asr.models.asr_model import ExportableEncDecModel
from nemo.collections.asr.parts.preprocessing.features import WaveformFeaturizer
from nemo.collections.asr.parts.preprocessing.perturb import process_augmentations
from nemo.collections.asr.parts.utils.speaker_utils import embedding_normalize, get_uniqname_from_filepath
from nemo.collections.asr.parts.utils.nmesc_clustering import get_argmin_mat
from nemo.collections.common.losses import CrossEntropyLoss as CELoss
from nemo.collections.common.metrics import TopKClassificationAccuracy
from nemo.collections.common.parts.preprocessing.collections import ASRSpeechLabel

from nemo.core.classes import ModelPT
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.core.neural_types import *
from nemo.utils import logging
from torchmetrics import Metric
from nemo.core.neural_types import (
    AcousticEncodedRepresentation,
    LengthsType,
    LogitsType,
    LogprobsType,
    EncodedRepresentation,
    NeuralType,
    SpectrogramType,
)
from nemo.core.neural_types.elements import ProbsType


def sprint(*args):
    # if False:
    if True:
        print(*args)
    else:
        pass

__all__ = ['EncDecDiarLabelModel', 'MultiBinaryAcc', 'ClusterEmbedding']

def write_json_file(name, lines):
    with open(name, 'w') as fout:
        for i, dic in enumerate(lines):
            json.dump(dic, fout)
            fout.write('\n')
    logging.info("wrote", name)

def getMultiScaleCosAffinityMatrix(uniq_embs_and_timestamps):
    """
    Calculate cosine similarity values among speaker embeddings for each scale then
    apply multiscale weights to calculate the fused similarity matrix.

    Args:
        uniq_embs_and_timestamps: (dict)
            The dictionary containing embeddings, timestamps and multiscale weights.
            If uniq_embs_and_timestamps contains only one scale, single scale diarization 
            is performed.

    Returns:
        fused_sim_d (np.array):
            This function generates an ffinity matrix that is obtained by calculating
            the weighted sum of the affinity matrices from the different scales.
        base_scale_emb (np.array):
            The base scale embedding (the embeddings from the finest scale)
    """
    uniq_scale_dict = uniq_embs_and_timestamps['scale_dict']
    base_scale_idx = max(uniq_scale_dict.keys())
    base_scale_emb = np.array(uniq_scale_dict[base_scale_idx]['embeddings'])
    multiscale_weights = uniq_embs_and_timestamps['multiscale_weights']
    scale_mapping_argmat = {}

    session_scale_mapping_dict = get_argmin_mat(uniq_scale_dict)
    for scale_idx in sorted(uniq_scale_dict.keys()):
        mapping_argmat = session_scale_mapping_dict[scale_idx]
        scale_mapping_argmat[scale_idx] = mapping_argmat
        # score_mat = getCosAffinityMatrix(uniq_scale_dict[scale_idx]['embeddings'])
        # score_mat_list.append(score_mat)
        # repeat_list = getRepeatedList(mapping_argmat, score_mat.shape[0])
        # repeated_mat = np.repeat(np.repeat(score_mat, repeat_list, axis=0), repeat_list, axis=1)
        # repeated_mat_list.append(repeated_mat)

    return scale_mapping_argmat

class MultiBinaryAcc(Metric):
    def __init__(self, dist_sync_on_step=False):
        super().__init__(dist_sync_on_step=dist_sync_on_step)
        self.correct_counts_k = 0
        self.total_counts_k = 0
        self.target_true = 0
        self.predicted_true = 0
        self.true_positive_count = 0
        self.false_positive_count = 0
        self.false_negative_count = 0
        

    def update(self, preds: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        with torch.no_grad():
            min_len = min(preds.shape[1], targets.shape[1])
            preds, targets = preds[:, :min_len, :], targets[:, :min_len, :]
            self.true = preds.round().bool() == targets.round().bool()
            self.false = preds.round().bool() != targets.round().bool()
            self.positive = preds.round().bool() == True
            self.negative = preds.round().bool() == False
            self.positive_count = torch.sum(preds.round().bool() == True)
            # self.true_positive_count += torch.sum(self.true == self.positive)
            # self.false_positive_count += torch.sum(self.false == self.positive)
            self.true_positive_count += torch.sum(torch.logical_and(self.true, self.positive))
            self.false_positive_count += torch.sum(torch.logical_and(self.false, self.positive))
            # self.false_negative_count += torch.sum(self.false == self.negative)
            self.false_negative_count += torch.sum(torch.logical_and(self.false, self.negative))
            self.correct_counts_k += torch.sum(preds.round().bool() == targets.round().bool())
            self.total_counts_k += torch.prod(torch.tensor(targets.shape))
            self.target_true += torch.sum(targets.round().bool()==True)
            self.predicted_true += torch.sum(preds.round().bool()==False)
            # print("correct_counts_k:", self.correct_counts_k, "self.total_counts_k", self.total_counts_k) 

    def compute(self):
        self.precision = self.true_positive_count / (self.true_positive_count + self.false_positive_count)
        self.recall = self.true_positive_count / (self.true_positive_count + self.false_negative_count)
        self.infer_positive_rate = self.positive_count/self.total_counts_k
        self.target_true_rate = self.target_true / self.total_counts_k
        # sprint("self.true_positive_count:", self.true_positive_count)
        # sprint("self.correct_counts_k:", self.correct_counts_k)
        # sprint("self.target_true:", self.target_true)
        # sprint("self.predicted_true:", self.predicted_true)
        print("[Metric] self.recall:", self.recall)
        print("[Metric] self.precision:", self.precision)
        self.f1_score = 2 * self.precision * self.recall / (self.precision + self.recall)
        self.f1_score = -1 if torch.isnan(self.f1_score) else self.f1_score
        print("[Metric] self.infer_positive_rate:", self.infer_positive_rate)
        print("[Metric] self.target_true_rate:", self.target_true_rate)
        print("[Metric] self.f1_score:", self.f1_score)
        return self.f1_score

class ClusterEmbedding:
    def __init__(self, cfg_clus: DictConfig):
        self._cfg = cfg_clus
        self._cfg_tsvad = cfg_clus.ts_vad_model
        self.max_num_of_spks = self._cfg.diarizer.clustering.parameters.max_num_speakers
        self.clus_emb_path = 'speaker_outputs/embeddings/clus_emb_info.pkl'
        self.clus_map_path = 'speaker_outputs/embeddings/clus_mapping.pkl'
        self.scale_map_path = 'speaker_outputs/embeddings/scale_mapping.pkl'
    
    def prepare_cluster_embs(self):
        """
        TSVAD
        Prepare embeddings from clustering diarizer for TS-VAD style diarizer.
        """
        self.emb_sess_train_dict, self.emb_seq_train, self.clus_train_label_dict = self.run_clustering_diarizer(self._cfg_tsvad.train_ds.manifest_filepath,
                                                                self._cfg_tsvad.train_ds.emb_dir)
        
        self.emb_sess_dev_dict, self.emb_seq_dev, self.clus_dev_label_dict = self.run_clustering_diarizer(self._cfg_tsvad.validation_ds.manifest_filepath,
                                                              self._cfg_tsvad.validation_ds.emb_dir)

        self.emb_sess_test_dict, self.emb_seq_test, self.clus_test_label_dict = self.run_clustering_diarizer(self._cfg_tsvad.test_ds.manifest_filepath,
                                                               self._cfg_tsvad.test_ds.emb_dir)


    # def prepare_split_manifest(self):
        # new_path_train = self.get_split_manifest(self._cfg_tsvad.train_ds.manifest_filepath,
                                                 # self._cfg_tsvad.train_ds.emb_dir)
        # self._cfg_tsvad.train_ds.manifest_filepath = new_path_train
        
        # new_path_dev = self.get_split_manifest(self._cfg_tsvad.validation_ds.manifest_filepath,
                                               # self._cfg_tsvad.validation_ds.emb_dir)
        # self._cfg_tsvad.validation_ds.manifest_filepath = new_path_dev
       
        # new_path_test = self.get_split_manifest(self._cfg_tsvad.test_ds.manifest_filepath,
                                                # self._cfg_tsvad.test_ds.emb_dir)
        # self._cfg_tsvad.test_ds.manifest_filepath = new_path_test

    # def get_split_manifest(self, org_manifest_filepath, emb_dir):
        # manifest_name = os.path.basename(org_manifest_filepath).split('.')[0]
        # _, json_lines_list = self.get_manifest_uniq_ids(org_manifest_filepath)
        # output_json_list = []
        # for json_dict in json_lines_list:
            # split_json_list = self.get_manifest_with_split_stamps(json_dict, self._cfg_tsvad.split_length)
            # output_json_list.extend(split_json_list)
        # new_manifest_path = f'{emb_dir}/{manifest_name}_TSVAD_split.json'
        # write_json_file(new_manifest_path, output_json_list)
        # return new_manifest_path
    
    # def get_manifest_with_split_stamps(self, meta, split_length):
        # meta_list = []
        # hop = split_length/2
        # wav_path = meta['audio_filepath']
        # if not os.path.exists(wav_path): 
            # raise FileNotFoundError(f"File does not exist: {wav_path}")

        # duration = float(subprocess.check_output("soxi -D {0}".format(wav_path), shell=True))
        # audio_clip_N = math.floor((duration-split_length)/hop) + 1
        # for i in range(audio_clip_N):
            # meta_dict = copy.deepcopy(meta)
            # meta_dict['offset'] = hop * i
            # meta_dict['duration'] = float(split_length)
            # meta_dict['text'] = "-"
            # meta_list.append(meta_dict)
            # # print(i,  meta_dict['offset'])
            # assert (meta_dict['offset'] + meta_dict['duration']) <= duration
        # return meta_list
    
    def assign_labels_to_longer_segs(self, scale_n, base_clus_label_dict, session_scale_mapping_dict):
        new_clus_label_dict = {scale_index: {} for scale_index in range(scale_n)}
        for uniq_id, uniq_scale_mapping_dict in session_scale_mapping_dict.items():
            try:
                base_scale_clus_label = np.array([ x[-1] for x in base_clus_label_dict[uniq_id]])
            except:
                import ipdb; ipdb.set_trace()

            new_clus_label_dict[scale_n-1][uniq_id] = base_scale_clus_label
            for scale_index in range(scale_n-1):
                new_clus_label = []
                for seg_idx in list(set(uniq_scale_mapping_dict[scale_index])):
                    seg_clus_label = mode(base_scale_clus_label[uniq_scale_mapping_dict[scale_index] == seg_idx])
                    new_clus_label.append(seg_clus_label)
                new_clus_label_dict[scale_index][uniq_id] = new_clus_label
        return new_clus_label_dict

    def get_clus_emb(self, emb_scale_seq_dict, clus_label, speaker_mapping_dict, session_scale_mapping_dict):
        """
        TSVAD
        Get an average embedding vector for each cluster (speaker).
        """
        scale_n = len(emb_scale_seq_dict.keys())
        base_clus_label_dict = {key: [] for key in emb_scale_seq_dict[scale_n-1].keys()}
        emb_sess_avg_dict = {scale_index:{key: [] for key in emb_scale_seq_dict[scale_n-1].keys() } for scale_index in emb_scale_seq_dict.keys()}
        for line in clus_label:
            uniq_id = line.split()[0]
            label = int(line.split()[-1].split('_')[-1])
            stt, end = [round(float(x), 2) for x in line.split()[1:3]]
            base_clus_label_dict[uniq_id].append([stt, end, label])
        
        all_scale_clus_label_dict = self.assign_labels_to_longer_segs(scale_n, base_clus_label_dict, session_scale_mapping_dict)
        dim = emb_scale_seq_dict[0][uniq_id][0].shape[0]
        for scale_index in emb_scale_seq_dict.keys():
            for uniq_id, emb_tensor in emb_scale_seq_dict[scale_index].items():
                clus_label_list = all_scale_clus_label_dict[scale_index][uniq_id]
                spk_set = set(clus_label_list)
                # Create a label array which identifies clustering result for each segment.
                spk_N = len(spk_set)
                assert spk_N <= self.max_num_of_spks, f"uniq_id {uniq_id} - self.max_num_of_spks {self.max_num_of_spks} is smaller than the actual number of speakers: {spk_N}"
                label_array = torch.Tensor(clus_label_list)
                avg_embs = torch.zeros(dim, self.max_num_of_spks)
                for spk_idx in spk_set:
                    selected_embs = emb_tensor[label_array == spk_idx]
                    avg_embs[:, spk_idx] = torch.mean(selected_embs, dim=0)
                inv_map = {clus_key: rttm_key for rttm_key, clus_key in speaker_mapping_dict[uniq_id].items()}
                emb_sess_avg_dict[scale_index][uniq_id] = {'mapping': inv_map, 'avg_embs': avg_embs}
        return emb_sess_avg_dict, base_clus_label_dict
    
    def get_manifest_uniq_ids(self, manifest_filepath):
        manifest_lines = []
        with open(manifest_filepath) as f:
            manifest_lines = f.readlines()
            for jsonObj in f:
                student_dict = json.loads(jsonObj)
                manifest_lines.append(student_dict)
        uniq_id_list, json_dict_list  = [], []
        for json_string in manifest_lines:
            json_dict = json.loads(json_string) 
            json_dict_list.append(json_dict)
            uniq_id = get_uniqname_from_filepath(json_dict['audio_filepath'])
            uniq_id_list.append(uniq_id)
        return uniq_id_list, json_dict_list
    
    def get_uniq_id(self, rttm_path):
        return rttm_path.split('/')[-1].split('.rttm')[0]
    
    def read_rttm_file(self, rttm_path):
        return open(rttm_path).readlines()
    
    def s2n(self, x, ROUND=2):
        return round(float(x), ROUND)
    
    def parse_rttm(self, rttm_path):
        rttm_lines = self.read_rttm_file(rttm_path)
        uniq_id = self.get_uniq_id(rttm_path)
        speaker_list = []
        for line in rttm_lines:
            rttm = line.strip().split()
            start, end, speaker = self.s2n(rttm[3]), self.s2n(rttm[4]) + self.s2n(rttm[3]), rttm[7]
            speaker_list.append(speaker)
        return set(speaker_list)

    def check_embedding_and_RTTM(self, emb_sess_avg_dict, manifest_filepath):
        uniq_id_list, json_lines_list = self.get_manifest_uniq_ids(manifest_filepath)
        output_json_list = []
        for scale_index in emb_sess_avg_dict.keys():
            for uniq_id, json_dict in zip(uniq_id_list, json_lines_list):
                rttm_filepath = json_dict['rttm_filepath']
                rttm_speaker_set = self.parse_rttm(rttm_filepath)
                dict_speaker_set = set(list(emb_sess_avg_dict[scale_index][uniq_id]['mapping'].keys()))
                dict_speaker_value_set = set(list(emb_sess_avg_dict[scale_index][uniq_id]['mapping'].values()))
                if rttm_speaker_set != dict_speaker_set:
                    remainder_rttm_keys = rttm_speaker_set - dict_speaker_set
                    total_spk_set = set(['speaker_'+str(x) for x in range(len(rttm_speaker_set))])
                    remainder_dict_keys = total_spk_set - dict_speaker_value_set
                    for rttm_key, dict_key in zip(remainder_rttm_keys, remainder_dict_keys):
                        emb_sess_avg_dict[scale_index][uniq_id]['mapping'][rttm_key] = dict_key
                    dict_speaker_set = set(list(emb_sess_avg_dict[scale_index][uniq_id]['mapping'].keys()))
                    assert rttm_speaker_set == dict_speaker_set
        return emb_sess_avg_dict

    def run_clustering_diarizer(self, manifest_filepath, out_dir):
        """
        TSVAD
        Run clustering diarizer to get initial clustering results.
        """
        isEmbReady = True
        if os.path.exists(f'{out_dir}/speaker_outputs/embeddings'):
            print(f"-- Embedding path exists {out_dir}/speaker_outputs/embeddings")
            try:
                emb_sess_avg_dict, session_scale_mapping_dict = self.load_dict_from_pkl(out_dir) 
                uniq_id_list, _ = self.get_manifest_uniq_ids(manifest_filepath)
                base_scale_index = max(emb_sess_avg_dict.keys())
                condA = set(uniq_id_list).issubset(set(emb_sess_avg_dict[base_scale_index].keys()))
                condB = set(uniq_id_list).issubset(set(session_scale_mapping_dict.keys()))
                isEmbReady = condA and condB
            except:
                # import ipdb; ipdb.set_trace()
                isEmbReady = False
        else:
            # import ipdb; ipdb.set_trace()
            isEmbReady = False
        
        if isEmbReady:    
            print(f"--- Embedding isEmbReady: {isEmbReady}")
            speaker_mapping_dict = self.load_mapping_from_pkl(out_dir) 
            emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict = self.load_embeddings_from_pickle(out_dir, 
                                                                                                      speaker_mapping_dict, 
                                                                                                      session_scale_mapping_dict)
        else:
            print("--- Embedding path does not exist")
            self._cfg.diarizer.manifest_filepath = manifest_filepath
            self._cfg.diarizer.out_dir = out_dir
            sd_model = ClusteringDiarizer(cfg=self._cfg)
            score = sd_model.diarize()
            metric, speaker_mapping_dict = score 
            session_scale_mapping_dict = self.get_scale_map(sd_model.embs_and_timestamps)
            emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict = self.load_embeddings_from_pickle(out_dir, 
                                                                                                      speaker_mapping_dict, 
                                                                                                      session_scale_mapping_dict)
            self.save_dict_as_pkl(out_dir, emb_sess_avg_dict, speaker_mapping_dict, session_scale_mapping_dict)

        logging.info("Checking clustering results and rttm files.")
        emb_sess_avg_dict = self.check_embedding_and_RTTM(emb_sess_avg_dict, manifest_filepath)
        logging.info("Clustering results and rttm files test passed.")
        emb_scale_seq_dict['session_scale_mapping'] = session_scale_mapping_dict
        return emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict
    
    def load_dict_from_pkl(self, out_dir): 
        with open(f'{out_dir}/{self.clus_emb_path}', 'rb') as handle:
            emb_sess_avg_dict = pkl.load(handle)
        with open(f'{out_dir}/{self.scale_map_path}', 'rb') as handle:
            session_scale_mapping_dict  = pkl.load(handle)
        return emb_sess_avg_dict, session_scale_mapping_dict
    
    def load_mapping_from_pkl(self, out_dir): 
        with open(f'{out_dir}/{self.clus_map_path}', 'rb') as handle:
            speaker_mapping_dict = pkl.load(handle)
        return speaker_mapping_dict

    def save_dict_as_pkl(self, out_dir, emb_sess_avg_dict, speaker_mapping_dict, session_scale_mapping_dict):
        with open(f'{out_dir}/{self.clus_emb_path}', 'wb') as handle:
            pkl.dump(emb_sess_avg_dict, handle, protocol=pkl.HIGHEST_PROTOCOL)
        with open(f'{out_dir}/{self.clus_map_path}', 'wb') as handle:
            pkl.dump(speaker_mapping_dict, handle, protocol=pkl.HIGHEST_PROTOCOL)
        with open(f'{out_dir}/{self.scale_map_path}', 'wb') as handle:
            pkl.dump(session_scale_mapping_dict, handle, protocol=pkl.HIGHEST_PROTOCOL)
    
    def get_scale_map(self, embs_and_timestamps):
        session_scale_mapping_dict = {}
        for uniq_id, uniq_embs_and_timestamps in embs_and_timestamps.items():
            scale_mapping_dict = getMultiScaleCosAffinityMatrix(uniq_embs_and_timestamps)
            session_scale_mapping_dict[uniq_id] = scale_mapping_dict
        return session_scale_mapping_dict
    
    def load_embeddings_from_pickle(self, out_dir, speaker_mapping_dict, session_scale_mapping_dict):
        """
        TSVAD
        Load embeddings from diarization result folder.
        """
        scale_index = 0
        window_len_list = list(self._cfg.diarizer.speaker_embeddings.parameters.window_length_in_sec)
        pickle_folder_path = os.path.join(out_dir, 'speaker_outputs', 'embeddings')
        emb_scale_seq_dict = {scale_index: None for scale_index in range(len(window_len_list))}
        for scale_index in range(len(window_len_list)):
            pickle_path = os.path.join(out_dir, 'speaker_outputs', 'embeddings', f'subsegments_scale{scale_index}_embeddings.pkl')
            with open(pickle_path, "rb") as input_file:
                emb_dict = pkl.load(input_file)
            for key, val in emb_dict.items():
                emb_dict[key] = torch.tensor(val)
            emb_scale_seq_dict[scale_index] = emb_dict
        base_scale_index = len(window_len_list) - 1
        clus_label_path = os.path.join(out_dir, 'speaker_outputs', f'subsegments_scale{base_scale_index}_cluster.label')
        with open(clus_label_path) as f:
            clus_label = f.readlines()
        emb_sess_avg_dict, base_clus_label_dict = self.get_clus_emb(emb_scale_seq_dict, clus_label, speaker_mapping_dict, session_scale_mapping_dict)
        return emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict

class EncDecDiarLabelModel(ModelPT, ExportableEncDecModel):
    """Encoder decoder class for speaker label models.
    Model class creates training, validation methods for setting up data
    performing model forward pass.
    Expects config dict for
    * preprocessor
    * Jasper/Quartznet Encoder
    * Speaker Decoder
    """

    @classmethod
    def list_available_models(cls) -> List[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.
        Returns:
            List of available pre-trained models.
        """
        result = []
        return None

    def __init__(self, cfg: DictConfig, emb_clus: Dict, trainer: Trainer = None):
        self.ts_vad_model_cfg = cfg
        cfg.tsvad_module.num_spks = cfg.max_num_of_spks
        cfg.train_ds.num_spks = cfg.max_num_of_spks
        cfg.validation_ds.num_spks = cfg.max_num_of_spks
        cfg.test_ds.num_spks = cfg.max_num_of_spks
        self.get_emb_clus(emb_clus)
        self.world_size = 1
        if trainer is not None:
            self.world_size = trainer.num_nodes * trainer.num_gpus

        super().__init__(cfg=cfg, trainer=trainer)
        self.preprocessor = EncDecDiarLabelModel.from_config_dict(cfg.preprocessor)
        self.tsvad = EncDecDiarLabelModel.from_config_dict(cfg.tsvad_module)
        self.loss = BCELoss()
        self.task = None
        self._accuracy = MultiBinaryAcc()
        self.labels = None

    def multispeaker_loss(self):
        """
        TSVAD
        Loss function for multispeaker loss
        """
        return torch.nn.BCELoss(reduction='sum')
    
    def get_emb_clus(self, emb_clus):
        self.emb_sess_train_dict = emb_clus.emb_sess_train_dict
        self.emb_sess_dev_dict = emb_clus.emb_sess_dev_dict
        self.emb_sess_test_dict = emb_clus.emb_sess_test_dict
        self.clus_train_label_dict = emb_clus.clus_train_label_dict
        self.clus_dev_label_dict = emb_clus.clus_dev_label_dict
        self.clus_test_label_dict = emb_clus.clus_test_label_dict
        self.emb_seq_train = emb_clus.emb_seq_train
        self.emb_seq_dev = emb_clus.emb_seq_dev
        self.emb_seq_test = emb_clus.emb_seq_test

    @staticmethod
    def extract_labels(data_layer_config):
        labels = set()
        manifest_filepath = data_layer_config.get('manifest_filepath', None)
        if manifest_filepath is None:
            logging.warning("No manifest_filepath was provided, no labels got extracted!")
            return None
        manifest_filepaths = convert_to_config_list(data_layer_config['manifest_filepath'])

        for manifest_filepath in itertools.chain.from_iterable(manifest_filepaths):
            collection = ASRSpeechLabel(
                manifests_files=manifest_filepath,
                min_duration=data_layer_config.get("min_duration", None),
                max_duration=data_layer_config.get("max_duration", None),
                index_by_file_id=True,  # Must set this so the manifest lines can be indexed by file ID
            )
            labels.update(collection.uniq_labels)
        labels = list(labels)
        logging.warning(f"Total number of {len(labels)} found in all the manifest files.")
        return labels

    def __setup_dataloader_from_config(self, config: Optional[Dict], emb_dict: Dict, emb_seq: Dict, clus_label_dict: Dict):
        if 'augmentor' in config:
            augmentor = process_augmentations(config['augmentor'])
        else:
            augmentor = None

        featurizer = WaveformFeaturizer(
            sample_rate=config['sample_rate'], int_values=config.get('int_values', False), augmentor=augmentor
        )
        shuffle = config.get('shuffle', False)
        if 'manifest_filepath' in config and config['manifest_filepath'] is None:
            logging.warning(f"Could not load dataset as `manifest_filepath` was None. Provided config : {config}")
            return None
        dataset = AudioToSpeechTSVADDataset(
            manifest_filepath=config['manifest_filepath'],
            emb_dict=emb_dict,
            clus_label_dict=clus_label_dict,
            emb_seq=emb_seq,
            featurizer=featurizer,
            subsample_rate=self.ts_vad_model_cfg.subsample_rate,
            max_spks=config.num_spks,
        )
        s0 = dataset.item_sim(0)
        s1 = dataset.item_sim(1)

        collate_ds = dataset
        collate_fn = collate_ds.tsvad_collate_fn
        packed_batch = list(zip(s0, s1))
        batch_size = config['batch_size']
        _dataloader = torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=batch_size,
            collate_fn=collate_fn,
            drop_last=config.get('drop_last', False),
            shuffle=shuffle,
            num_workers=config.get('num_workers', 0),
            pin_memory=config.get('pin_memory', False),
        )
        # ff, ffl, tt, iiv = next(iter(_dataloader))
        return torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=batch_size,
            collate_fn=collate_fn,
            drop_last=config.get('drop_last', False),
            shuffle=shuffle,
            num_workers=config.get('num_workers', 0),
            pin_memory=config.get('pin_memory', False),
        )

    def setup_training_data(self, train_data_config: Optional[Union[DictConfig, Dict]]):
        # self.labels = self.extract_labels(train_data_config)
        # train_data_config['labels'] = self.labels
        # if 'shuffle' not in train_data_config:
            # train_data_config['shuffle'] = True
        self._train_dl = self.__setup_dataloader_from_config(config=train_data_config, 
                                                             emb_dict=self.emb_sess_train_dict, 
                                                             emb_seq=self.emb_seq_train,
                                                             clus_label_dict=self.clus_train_label_dict)

    def setup_validation_data(self, val_data_layer_config: Optional[Union[DictConfig, Dict]]):
        # val_data_layer_config['labels'] = self.labels
        # self.task = 'identification'
        self._validation_dl = self.__setup_dataloader_from_config(config=val_data_layer_config, 
                                                                  emb_dict=self.emb_sess_dev_dict, 
                                                                  emb_seq=self.emb_seq_dev,
                                                                  clus_label_dict=self.clus_dev_label_dict)

    def setup_test_data(self, test_data_config: Optional[Union[DictConfig, Dict]]):
        # if hasattr(self, 'dataset'):
            # test_data_config['labels'] = self.labels
        self._test_dl = self.__setup_dataloader_from_config(config=test_data_config, 
                                                            emb_dict=self.emb_sess_test_dict, 
                                                            emb_seq=self.emb_seq_test,
                                                            clus_label_dict=self.clus_test_label_dict)

    def test_dataloader(self):
        if self._test_dl is not None:
            return self._test_dl

    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        if hasattr(self.preprocessor, '_sample_rate'):
            audio_eltype = AudioSignal(freq=self.preprocessor._sample_rate)
        else:
            audio_eltype = AudioSignal()
        return {
            "input_signal": NeuralType(('B', 'T'), audio_eltype),
            "input_signal_length": NeuralType(tuple('B'), LengthsType()),
            "ivectors": NeuralType(('B', 'D', 'C'), EncodedRepresentation()),
        }

    @property
    def output_types(self):
        return OrderedDict({"probs": NeuralType(('B', 'T', 'C'), LogprobsType())})

    @typecheck()
    def forward(self, input_signal, input_signal_length, ivectors):
        length=3000
        sprint("EncDecDiarLabelModel.forward.. ")
        # sprint("self.tsvad.input_types:", self.tsvad.input_types)
        processed_signal, processed_signal_len = self.preprocessor(
            input_signal=input_signal, length=input_signal_length,
        )
        # print("processed_signal.shape:", processed_signal.shape)
        # print("processed_signal_len:", processed_signal_len)
        processed_signal = processed_signal[:, :, :length]
        processed_signal_len = length*torch.ones_like(processed_signal_len)
        preds = self.tsvad(audio_signal=processed_signal, length=processed_signal_len, ivectors=ivectors)
        return preds

    # PTL-specific methods
    def training_step(self, batch, batch_idx):
        sprint(f"Running Training  Step.... batch_idx {batch_idx}")

        sprint("Running Training Step 1....")
        signals, signal_lengths, targets, ivectors = batch
        sprint("Running Training Step 2....")
        preds = self.forward(input_signal=signals, 
                             input_signal_length=signal_lengths, 
                             ivectors=ivectors)
        sprint("Running Training Step 3....")
        loss = self.loss(logits=preds, labels=targets)

        self.log('loss', loss)
        self.log('learning_rate', self._optimizer.param_groups[0]['lr'])

        sprint("Running Training Step 4....")
        # import ipdb; ipdb.set_trace()
        # sprint("preds:", preds)
        # sprint("target:", targets)
        self._accuracy(preds, targets)
        acc = self._accuracy.compute()
        sprint("Running Training Step 5....")
        self._accuracy.reset()
        self.log(f'training_batch_accuracy', acc)
        sprint("Accuracy: ", acc)
        # logging.info(f"Accuracy: {acc}")
        return {'loss': loss}

    def validation_step(self, batch, batch_idx, dataloader_idx: int = 0):
        sprint("batch data size : ", len(batch), [x.shape for x in batch])
        # import ipdb; ipdb.set_trace()
        sprint(f"Running Validation Step0.... batch_idx {batch_idx} dataloader_idx {dataloader_idx} ")
        signals, signal_lengths, targets, ivectors = batch
        sprint(f"Running Validation Step1.... batch_idx {batch_idx} dataloader_idx {dataloader_idx} ")
        sprint(signals.shape, signal_lengths, ivectors.shape, targets.shape)
        preds = self.forward(input_signal=signals, 
                             input_signal_length=signal_lengths, 
                             ivectors=ivectors)
        sprint(f"Running Validation Step2.... batch_idx {batch_idx} dataloader_idx {dataloader_idx} ")
        loss_value = self.loss(logits=preds, labels=targets)
        self._accuracy(preds, targets)
        acc = self._accuracy.compute()
        sprint(f"Running Validation Step3.... batch_idx {batch_idx} dataloader_idx {dataloader_idx} ")
        correct_counts, total_counts = self._accuracy.correct_counts_k, self._accuracy.total_counts_k

        return {
            'val_loss': loss_value,
            'val_correct_counts': correct_counts,
            'val_total_counts': total_counts,
            'val_acc': acc,
        }

    def multi_validation_epoch_end(self, outputs, dataloader_idx: int = 0):
        val_loss_mean = torch.stack([x['val_loss'] for x in outputs]).mean()
        correct_counts = torch.stack([x['val_correct_counts'] for x in outputs]).sum(axis=0)
        total_counts = torch.stack([x['val_total_counts'] for x in outputs]).sum(axis=0)

        self._accuracy.correct_counts_k = correct_counts
        self._accuracy.total_counts_k = total_counts
        acc = self._accuracy.compute()
        self._accuracy.reset()

        # logging.info("val_loss: {:.3f}".format(val_loss_mean))
        self.log('val_loss', val_loss_mean)
        self.log('training_batch_accuracy', acc)

        return {
            'val_loss': val_loss_mean,
            'val_acc': acc,
        }

    def test_step(self, batch, batch_idx, dataloader_idx: int = 0):
        signals, signal_lengths, targets, ivectors = batch
        preds = self.forward(input_signal=signals, 
                             input_signal_length=signal_lengths, 
                             ivectors=ivectors)
        loss_value = self.loss(preds, targets)
        self._accuracy(preds, targets)
        acc = self._accuracy.compute()
        correct_counts, total_counts = self._accuracy.correct_counts_k, self._accuracy.total_counts_k

        return {
            'test_loss': loss_value,
            'test_correct_counts': correct_counts,
            'test_total_counts': total_counts,
            'test_acc_top_k': acc,
        }

    def multi_test_epoch_end(self, outputs, dataloader_idx: int = 0):
        test_loss_mean = torch.stack([x['test_loss'] for x in outputs]).mean()
        correct_counts = torch.stack([x['test_correct_counts'] for x in outputs]).sum(axis=0)
        total_counts = torch.stack([x['test_total_counts'] for x in outputs]).sum(axis=0)

        self._accuracy.correct_counts_k = correct_counts
        self._accuracy.total_counts_k = total_counts
        acc = self._accuracy.compute()
        self._accuracy.reset()

        logging.info("test_loss: {:.3f}".format(test_loss_mean))
        self.log('test_loss', test_loss_mean)

        return {
            'test_loss': test_loss_mean,
            'test_acc_top_k': acc,
        }

