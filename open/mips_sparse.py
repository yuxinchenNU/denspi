from mips import MIPS, int8_to_float, adjust
from scipy.sparse import vstack

import scipy.sparse as sp
import numpy as np
import os
import h5py
import re


def dequant(group, input_):
    if 'offset' in group.attrs:
        return int8_to_float(input_, group.attrs['offset'], group.attrs['scale'])
    return input_


def linear_mxq(q_idx, q_val, c_idx, c_val):
    q_dict = {}
    for idx, val in zip(q_idx, q_val):
        if val <= 0:
            continue
        if idx not in q_dict:
            q_dict[idx] = [val, 0.0]
        else:
            q_dict[idx][0] += val

    for idx, val in zip(c_idx, c_val):
        if idx in q_dict:
            q_dict[idx][1] += val

    total = sum([a[0]*a[1] for a in q_dict.values()])
    return total


class MIPSSparse(MIPS):
    def __init__(self, phrase_dump_dir, start_index_path, idx2id_path, max_answer_length, para=False,
                 tfidf_dump_dir=None, sparse_weight=1e-1, ranker=None, doc_mat=None, sparse_type=None):
        super(MIPSSparse, self).__init__(phrase_dump_dir, start_index_path, idx2id_path, max_answer_length, para)
        assert os.path.isdir(tfidf_dump_dir)
        self.tfidf_dump_paths = sorted([os.path.join(tfidf_dump_dir, name) for name in os.listdir(tfidf_dump_dir) if 'hdf5' in name])
        dump_names = [os.path.splitext(os.path.basename(path))[0] for path in self.tfidf_dump_paths]
        dump_ranges = [list(map(int, name.split('_')[0].split('-'))) for name in dump_names]
        self.tfidf_dumps = [h5py.File(path, 'r') for path in self.tfidf_dump_paths]
        assert dump_ranges == self.dump_ranges
        self.sparse_weight = sparse_weight
        self.ranker = ranker
        self.doc_mat = doc_mat
        self.hash_size = self.doc_mat.shape[1]
        self.sparse_type = sparse_type

    def get_tfidf_group(self, doc_idx):
        if len(self.tfidf_dumps) == 1:
            return self.tfidf_dumps[0][str(doc_idx)]
        for dump_range, dump in zip(self.dump_ranges, self.tfidf_dumps):
            if dump_range[0] * 1000 <= int(doc_idx) < dump_range[1] * 1000:
                return dump[str(doc_idx)]
        raise ValueError('%d not found in dump list' % int(doc_idx))

    def search_start(self, query_start, doc_idxs=None, para_idxs=None,
                     start_top_k=100, out_top_k=5, nprobe=16, q_texts=None):
        # doc_idxs = [Q], para_idxs = [Q]
        assert self.start_index is not None
        query_start = query_start.astype(np.float32)

        # Open
        if doc_idxs is None:
            # Search space reduction with Faiss
            query_start = np.concatenate([np.zeros([query_start.shape[0], 1]).astype(np.float32), 
                                          query_start], axis=1)
            if self.num_dummy_zeros > 0:
                query_start = np.concatenate([query_start, np.zeros([query_start.shape[0], self.num_dummy_zeros],
                                                                    dtype=query_start.dtype)], axis=1)
            self.start_index.nprobe = nprobe
            start_scores, I = self.start_index.search(query_start, start_top_k)

            doc_idxs = self.idx2doc_id[I]
            start_idxs = self.idx2word_id[I]
            if self.para:
                para_idxs = self.idx2para_id[I]

            # Only top faiss for profiling
            '''
            doc_idxs = doc_idxs[:,:out_top_k]
            start_idxs = start_idxs[:,:out_top_k]
            if self.para:
                para_idxs = para_idxs[:,:out_top_k]
            start_scores = start_scores[:,:out_top_k]

            '''
            # Rerank based on sparse + dense (start)
            query_start = np.reshape(np.tile(np.expand_dims(query_start[:,1:], 1), 
                                     [1, start_top_k, 1]), [-1, query_start[:,1:].shape[1]])
            doc_idxs = np.reshape(doc_idxs, [-1])
            if self.para:
                para_idxs = np.reshape(para_idxs, [-1])
            start_idxs = np.reshape(start_idxs, [-1])
            groups = [self.get_doc_group(doc_idx) for doc_idx in doc_idxs]

            if self.para:
                groups = [group[str(para_idx)] for group, para_idx in zip(groups, para_idxs)]
            else:
                if 'p' in self.sparse_type:
                    doc_bounds = [[m.start() for m in re.finditer('\[PAR\]', group.attrs['context'])] for group in groups]
                    doc_starts = [group['word2char_start'][start_idx].item() for group, start_idx in zip(groups, start_idxs)]
                    para_idxs = [sum([1 if start > bound else 0 for bound in par_bound])
                                 for par_bound, start in zip(doc_bounds, doc_starts)]

            # Get Q vec, dense vec
            if not len(self.sparse_type) == 0:
                q_spvecs = vstack([self.ranker.text2spvec(q) for q in q_texts])
            start = np.stack([group['start'][start_idx, :]
                              for group, start_idx in zip(groups, start_idxs)], 0)  # [Q, d]
            start = dequant(groups[0], start)
            start_scores = np.sum(query_start * start, 1)  # [Q]

            # Get doc vec
            if 'd' in self.sparse_type:
                doc_spvecs = self.doc_mat[doc_idxs, :]
                doc_scores = np.squeeze((doc_spvecs * q_spvecs.T).toarray())
                start_scores += doc_scores * self.sparse_weight

            # Get par vec
            if 'p' in self.sparse_type:
                tfidf_groups = [self.get_tfidf_group(doc_idx) for doc_idx in doc_idxs]
                tfidf_groups = [group[str(para_idx)] for group, para_idx in zip(tfidf_groups, para_idxs)]
                par_spvecs = vstack([sp.csr_matrix((data['vals'], data['idxs'], np.array([0, len(data['idxs'])])),
                                     shape=(1,self.hash_size))
                                     for data in tfidf_groups])
                par_scores = np.squeeze((par_spvecs * q_spvecs.T).toarray())
                start_scores += par_scores * self.sparse_weight

            rerank_scores = np.reshape(start_scores, [-1, start_top_k])
            rerank_idxs = np.array([scores.argsort()[-out_top_k:][::-1]
                                    for scores in rerank_scores])
            new_I = np.array([each_I[idxs] for each_I, idxs in zip(I, rerank_idxs)])

            doc_idxs = self.idx2doc_id[new_I]
            start_idxs = self.idx2word_id[new_I]
            if self.para:
                para_idxs = self.idx2para_id[new_I]
            
            start_scores = np.array([scores[idxs] for scores, idxs in zip(rerank_scores, rerank_idxs)])[:,:out_top_k]

        # Closed
        else:
            groups = [self.get_doc_group(doc_idx)[str(para_idx)] for doc_idx, para_idx in zip(doc_idxs, para_idxs)]
            starts = [group['start'][:, :] for group in groups]
            starts = [int8_to_float(start, groups[0].attrs['offset'], groups[0].attrs['scale']) for start in starts]
            all_scores = [np.squeeze(np.matmul(start, query_start[i:i + 1, :].transpose()), -1)
                          for i, start in enumerate(starts)]
            start_idxs = np.array([scores.argsort()[-out_top_k:][::-1]
                                   for scores in all_scores])
            start_scores = np.array([scores[idxs] for scores, idxs in zip(all_scores, start_idxs)])
            doc_idxs = np.tile(np.expand_dims(doc_idxs, -1), [1, out_top_k])
            para_idxs = np.tile(np.expand_dims(para_idxs, -1), [1, out_top_k])
        return start_scores, doc_idxs, para_idxs, start_idxs


    # Just added q_sparse / q_input_ids to pass to search_phrase
    def search(self, query, top_k=5, nprobe=64, doc_idxs=None, para_idxs=None, start_top_k=100, q_texts=None):
        num_queries = query.shape[0]
        bs = int((query.shape[1] - 1) / 2)
        query_start = query[:, :bs]
        start_scores, doc_idxs, para_idxs, start_idxs = self.search_start(query_start, start_top_k=start_top_k, 
                out_top_k=top_k, nprobe=nprobe, doc_idxs=doc_idxs, para_idxs=para_idxs, q_texts=q_texts)

        if doc_idxs.shape[1] != top_k:
            print("Warning.. %d only retrieved" % doc_idxs.shape[1])
            top_k = doc_idxs.shape[1]

        # reshape
        query = np.reshape(np.tile(np.expand_dims(query, 1), [1, top_k, 1]), [-1, query.shape[1]])
        idxs = np.reshape(np.tile(np.expand_dims(np.arange(num_queries), 1), [1, top_k]), [-1])
        start_scores = np.reshape(start_scores, [-1])
        doc_idxs = np.reshape(doc_idxs, [-1])
        para_idxs = np.reshape(para_idxs, [-1])
        start_idxs = np.reshape(start_idxs, [-1])

        out = self.search_phrase(query, doc_idxs, start_idxs, para_idxs=para_idxs, start_scores=start_scores)
        # out = self.search_phrase(query, doc_idxs, start_idxs, para_idxs=para_idxs)
        new_out = [[] for _ in range(num_queries)]
        for idx, each_out in zip(idxs, out):
            new_out[idx].append(each_out)
        for i in range(len(new_out)):
            new_out[i] = sorted(new_out[i], key=lambda each_out: -each_out['score'])

        return new_out
