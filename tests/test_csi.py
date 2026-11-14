import numpy as np
import pytest

from app.ml.csi import (
    CsiFormatError,
    frames_to_arrays,
    iq_to_amplitude,
    pack_session_csv,
    parse_esp_csi_line,
    read_session,
    read_session_csv,
)

HEADER = ("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,"
          "sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,"
          "first_word,data")


def test_amplitude_uses_imag_real_pairs():
    # [imag0, real0, imag1, real1]
    amp = iq_to_amplitude([4, 3, 0, -5])
    assert np.allclose(amp, [5.0, 5.0])


@pytest.mark.parametrize("raw", [[], [1, 2, 3]])
def test_amplitude_rejects_bad_length(raw):
    with pytest.raises(CsiFormatError):
        iq_to_amplitude(raw)


def test_parse_line_with_header():
    line = 'CSI_DATA,12,aa:bb:cc:dd:ee:ff,-47,11,1,7,0,0,0,0,0,0,0,-95,0,6,1,123456,0,96,0,4,0,"[1,2,3,4]"'
    rssi, ts, raw = parse_esp_csi_line(line, HEADER.split(","))
    assert rssi == -47
    assert ts == 123456
    assert raw == [1, 2, 3, 4]


def test_parse_line_without_header_reads_rssi():
    line = 'CSI_DATA,1,aa:bb:cc:dd:ee:ff,-52,11,"[0,1]"'
    rssi, ts, raw = parse_esp_csi_line(line)
    assert rssi == -52 and ts is None and raw == [0, 1]


def test_non_csi_lines_are_ignored():
    assert parse_esp_csi_line("I (123) wifi: connected") is None


def test_read_both_file_formats(tmp_path):
    ours = tmp_path / "s.csv"
    ours.write_text('t,rssi,data\n1.0,-40,"[3,4,0,1]"\n1.02,-41,"[3,4,0,2]"\n', encoding="utf-8")
    frames = read_session_csv(ours)
    assert len(frames) == 2 and frames[0].t == 1.0 and np.allclose(frames[0].amp, [5.0, 1.0])

    raw_log = tmp_path / "raw.log"
    raw_log.write_text(
        HEADER + "\n"
        'CSI_DATA,1,aa:bb:cc:dd:ee:ff,-47,11,1,7,0,0,0,0,0,0,0,-95,0,6,1,1000000,0,96,0,4,0,"[3,4,0,1]"\n'
        'CSI_DATA,2,aa:bb:cc:dd:ee:ff,-48,11,1,7,0,0,0,0,0,0,0,-95,0,6,1,1020000,0,96,0,4,0,"[3,4,0,1]"\n',
        encoding="utf-8",
    )
    frames = read_session_csv(raw_log)
    assert [round(f.t, 3) for f in frames] == [0.0, 0.02]


def test_packed_session_matches_csv(tmp_path):
    # 묶음 파일(.npz)은 CSV와 같은 값을 담아야 한다. 길이가 다른 줄은 양쪽 모두에서 빠진다.
    src = tmp_path / "s.csv"
    src.write_text(
        't,rssi,data\n'
        '1794122335.0612,-53.0,"[3,4,0,1,-1317,1101]"\n'
        '1794122335.0731,-54.0,"[3,4,0,2,5,-12]"\n'
        '1794122335.0850,-53.0,"[1,2,3,4]"\n'
        '1794122335.0969,,"[0,0,7,-24,9,40]"\n',
        encoding="utf-8",
    )
    packed = tmp_path / "s.npz"
    assert pack_session_csv(src, packed) == (3, 1)

    t0, rssi0, amp0 = frames_to_arrays(read_session_csv(src))
    t1, rssi1, amp1 = frames_to_arrays(read_session(packed))
    assert np.array_equal(t0, t1)
    assert np.array_equal(rssi0, rssi1, equal_nan=True)
    assert np.array_equal(amp0, amp1)
    assert read_session(src)[0].t == read_session_csv(src)[0].t


def test_packed_session_rejects_empty_csv(tmp_path):
    src = tmp_path / "empty.csv"
    src.write_text("t,rssi,data\n", encoding="utf-8")
    with pytest.raises(CsiFormatError):
        pack_session_csv(src, tmp_path / "empty.npz")
