"""the text encoder lets go of the towers it never runs (images, sound) and keeps the rest"""
import torch

import enc_eg2


class _Model(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.language_model = torch.nn.Linear(4, 4)
        self.vision_tower = torch.nn.Linear(8, 8)
        self.audio_tower = torch.nn.Linear(16, 16)


class _Wrap:
    def __init__(self):
        self.auto_model = _Model()


def test_towers_are_dropped_and_the_text_part_stays():
    st = [_Wrap()]
    freed = enc_eg2.drop_unused_towers(st)
    am = st[0].auto_model
    assert freed == 8 * 8 + 8 + 16 * 16 + 16
    assert am.vision_tower is None and am.audio_tower is None and am.language_model is not None
    assert enc_eg2.drop_unused_towers(st) == 0          # nothing left to drop


def test_a_model_without_towers_is_left_alone():
    class Plain:
        auto_model = torch.nn.Linear(2, 2)
    assert enc_eg2.drop_unused_towers([Plain()]) == 0
    assert enc_eg2.drop_unused_towers([]) == 0           # an unexpected shape: nothing happens
