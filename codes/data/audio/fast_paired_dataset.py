import os
import os
import random
import sys

import torch
import torch.nn.functional as F
import torch.utils.data
import torchaudio
from tqdm import tqdm

from data.audio.paired_voice_audio_dataset import CharacterTokenizer
from data.audio.unsupervised_audio_dataset import load_audio, load_similar_clips
from models.tacotron2.taco_utils import load_filepaths_and_text
from models.tacotron2.text import text_to_sequence, sequence_to_text
from utils.util import opt_get


def parse_tsv_aligned_codes(line, base_path):
    fpt = line.strip().split('\t')
    def convert_string_list_to_tensor(strlist):
        if strlist.startswith('['):
            strlist = strlist[1:]
        if strlist.endswith(']'):
            strlist = strlist[:-1]
        as_ints = [int(s) for s in strlist.split(', ')]
        return torch.tensor(as_ints)
    return os.path.join(base_path, f'{fpt[1]}'), fpt[0], convert_string_list_to_tensor(fpt[2])


class FastPairedVoiceDataset(torch.utils.data.Dataset):
    """
    This dataset is derived from paired_voice_audio, but it only supports loading from TSV files generated from the
    ocotillo transcription engine, which includes alignment codes. To support the vastly larger TSV files, this dataset
    uses an indexing mechanism which randomly selects offsets within the translation file to seek to. The data returned
    is relative to these offsets.

    In practice, this means two things:
    1) Index {i} of this dataset means nothing: fetching from the same index will almost always return different data.
       As a result, this dataset should not be used for validation or test runs.
    2) This dataset has a slight bias for items with longer text or longer filenames.

    The upshot is that this dataset loads extremely quickly and consumes almost no system memory.
    """
    def __init__(self, hparams):
        self.paths = hparams['path']
        if not isinstance(self.paths, list):
            self.paths = [self.paths]
        self.paths_size_bytes = [os.path.getsize(p) for p in self.paths]
        self.total_size_bytes = sum(self.paths_size_bytes)

        self.load_conditioning = opt_get(hparams, ['load_conditioning'], False)
        self.conditioning_candidates = opt_get(hparams, ['num_conditioning_candidates'], 1)
        self.conditioning_length = opt_get(hparams, ['conditioning_length'], 44100)
        self.debug_failures = opt_get(hparams, ['debug_loading_failures'], False)
        self.aligned_codes_to_audio_ratio = opt_get(hparams, ['aligned_codes_ratio'], 443)
        self.text_cleaners = hparams.text_cleaners
        self.sample_rate = hparams.sample_rate
        self.max_wav_len = opt_get(hparams, ['max_wav_length'], None)
        if self.max_wav_len is not None:
            self.max_aligned_codes = self.max_wav_len // self.aligned_codes_to_audio_ratio
        self.max_text_len = opt_get(hparams, ['max_text_length'], None)
        assert self.max_wav_len is not None and self.max_text_len is not None
        self.use_bpe_tokenizer = opt_get(hparams, ['use_bpe_tokenizer'], False)
        if self.use_bpe_tokenizer:
            from data.audio.voice_tokenizer import VoiceBpeTokenizer
            self.tokenizer = VoiceBpeTokenizer(opt_get(hparams, ['tokenizer_vocab'], '../experiments/bpe_lowercase_asr_256.json'))
        else:
            self.tokenizer = CharacterTokenizer()
        self.skipped_items = 0  # records how many items are skipped when accessing an index.

    def get_wav_text_pair(self, audiopath_and_text):
        # separate filename and text
        audiopath, text = audiopath_and_text[0], audiopath_and_text[1]
        text_seq = self.get_text(text)
        wav = load_audio(audiopath, self.sample_rate)
        return (text_seq, wav, text, audiopath_and_text[0])

    def get_text(self, text):
        tokens = self.tokenizer.encode(text)
        tokens = torch.IntTensor(tokens)
        if self.use_bpe_tokenizer:
            # Assert if any UNK,start tokens encountered.
            assert not torch.any(tokens == 1)
        # The stop token should always be sacred.
        assert not torch.any(tokens == 0)
        return tokens

    def load_random_line(self, depth=0):
        assert depth < 10

        rand_offset = random.randint(0, self.total_size_bytes)
        for i in range(len(self.paths)):
            if rand_offset < self.paths_size_bytes[i]:
                break
            else:
                rand_offset -= self.paths_size_bytes[i]
        path = self.paths[i]
        with open(path, 'r', encoding='utf-8') as f:
            f.seek(rand_offset)
            # Read the rest of the line we seeked to, then the line after that.
            try:  # This can fail when seeking to a UTF-8 escape byte.
                f.readline()
            except:
                return self.load_random_line(depth=depth + 1)  # On failure, just recurse and try again.
            l2 = f.readline()

        if l2:
            try:
                base_path = os.path.dirname(path)
                return parse_tsv_aligned_codes(l2, base_path)
            except:
                print(f"error parsing random offset: {sys.exc_info()}")
        return self.load_random_line(depth=depth+1)  # On failure, just recurse and try again.


    def __getitem__(self, index):
        self.skipped_items += 1
        apt = self.load_random_line()
        try:
            tseq, wav, text, path = self.get_wav_text_pair(apt)
            if text is None or len(text.strip()) == 0:
                raise ValueError
            cond, cond_is_self = load_similar_clips(apt[0], self.conditioning_length, self.sample_rate,
                                      n=self.conditioning_candidates) if self.load_conditioning else (None, False)
        except:
            if self.skipped_items > 100:
                raise  # Rethrow if we have nested too far.
            if self.debug_failures:
                print(f"error loading {apt[0]} {sys.exc_info()}")
            return self[(index+1) % len(self)]
        aligned_codes = apt[2]

        actually_skipped_items = self.skipped_items
        self.skipped_items = 0
        if wav is None or \
            (self.max_wav_len is not None and wav.shape[-1] > self.max_wav_len) or \
            (self.max_text_len is not None and tseq.shape[0] > self.max_text_len):
            # Basically, this audio file is nonexistent or too long to be supported by the dataset.
            # It's hard to handle this situation properly. Best bet is to return the a random valid token and skew the dataset somewhat as a result.
            if self.debug_failures:
                print(f"error loading {path}: ranges are out of bounds; {wav.shape[-1]}, {tseq.shape[0]}")
            rv = random.randint(0,len(self)-1)
            return self[rv]
        orig_output = wav.shape[-1]
        orig_text_len = tseq.shape[0]
        orig_aligned_code_length = aligned_codes.shape[0]
        if wav.shape[-1] != self.max_wav_len:
            wav = F.pad(wav, (0, self.max_wav_len - wav.shape[-1]))
            # These codes are aligned to audio inputs, so make sure to pad them as well.
            aligned_codes = F.pad(aligned_codes, (0, self.max_aligned_codes-aligned_codes.shape[0]))
        if tseq.shape[0] != self.max_text_len:
            tseq = F.pad(tseq, (0, self.max_text_len - tseq.shape[0]))
        res = {
            'real_text': text,
            'padded_text': tseq,
            'aligned_codes': aligned_codes,
            'aligned_codes_lengths': orig_aligned_code_length,
            'text_lengths': torch.tensor(orig_text_len, dtype=torch.long),
            'wav': wav,
            'wav_lengths': torch.tensor(orig_output, dtype=torch.long),
            'filenames': path,
            'skipped_items': actually_skipped_items,
        }
        if self.load_conditioning:
            res['conditioning'] = cond
            res['conditioning_contains_self'] = cond_is_self
        return res

    def __len__(self):
        return self.total_size_bytes // 1000  # 1000 cuts down a TSV file to the actual length pretty well.


if __name__ == '__main__':
    batch_sz = 16
    params = {
        'mode': 'fast_paired_voice_audio',
        'path': ['Y:\\libritts\\train-clean-360\\transcribed-w2v.tsv', 'Y:\\clips\\books1\\transcribed-w2v.tsv'],
        'phase': 'train',
        'n_workers': 0,
        'batch_size': batch_sz,
        'max_wav_length': 255995,
        'max_text_length': 200,
        'sample_rate': 22050,
        'load_conditioning': True,
        'num_conditioning_candidates': 1,
        'conditioning_length': 44000,
        'use_bpe_tokenizer': False,
        'load_aligned_codes': True,
    }
    from data import create_dataset, create_dataloader

    def save(b, i, ib, key, c=None):
        if c is not None:
            torchaudio.save(f'{i}_clip_{ib}_{key}_{c}.wav', b[key][ib][c], 22050)
        else:
            torchaudio.save(f'{i}_clip_{ib}_{key}.wav', b[key][ib], 22050)

    ds, c = create_dataset(params, return_collate=True)
    dl = create_dataloader(ds, params, collate_fn=c)
    i = 0
    m = None
    for i, b in tqdm(enumerate(dl)):
        for ib in range(batch_sz):
            print(f'{i} {ib} {b["real_text"][ib]}')
            save(b, i, ib, 'wav')
        if i > 5:
            break

