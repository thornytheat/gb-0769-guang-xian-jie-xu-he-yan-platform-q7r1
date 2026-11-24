from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'sample_data'
SPEC = 'splice-1.0'
SOURCES = ('a', 'b', 'design')
# 纠正单交换的远端测量原件（B 端）；A 端原件与设计清单各线路自留
REMOTE_SOURCE = 'b'
MAX_ROWS = 350
