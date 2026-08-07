"""
audio.features.pitch — 音域分析
"""
import librosa

from ..models import PitchRange


def _midi_to_note(midi: int) -> str:
    """MIDI 编号 → 音名 (含八度)"""
    NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    octave = midi // 12 - 1
    note = NOTES[midi % 12]
    return f"{note}{octave}"


def get_pitch_range(y, sr) -> PitchRange:
    """音域分析: 检测最低/最高音"""
    # 使用 pyin 检测音高 (比 piptrack 准, 慢一点)
    try:
        f0, voiced_flag, _ = librosa.pyin(
            y, fmin=librosa.note_to_hz('C2'), fmax=librosa.note_to_hz('C6'), sr=sr
        )
        # 只取有声段
        voiced_f0 = f0[voiced_flag] if voiced_flag is not None else f0[~np.isnan(f0)] if f0 is not None else None
    except Exception:
        # 退化方案: 用 piptrack
        pitches, magnitudes = librosa.piptrack(y=y, sr=sr)
        voiced_f0 = []
        for t in range(pitches.shape[1]):
            index = magnitudes[:, t].argmax()
            pitch = pitches[index, t]
            if pitch > 0:
                voiced_f0.append(pitch)

    if voiced_f0 is None or len(voiced_f0) == 0:
        return PitchRange(
            low_midi=0, high_midi=0,
            low_note="—", high_note="—",
            range_semitones=0,
        )

    voiced_f0 = np.array([f for f in voiced_f0 if f > 0])
    if len(voiced_f0) == 0:
        return PitchRange(0, 0, "—", "—", 0)

    # MIDI 转换
    midis = librosa.hz_to_midi(voiced_f0)
    low_midi = int(np.min(midis))
    high_midi = int(np.max(midis))

    return PitchRange(
        low_midi=low_midi,
        high_midi=high_midi,
        low_note=_midi_to_note(low_midi),
        high_note=_midi_to_note(high_midi),
        range_semitones=int(high_midi - low_midi),
    )


import numpy as np  # 放最后避免循环导入