"""``generate(seed=7)`` is byte-identical across two processes: every written file and a digest of
every canonical record and of the truth (SPEC §2.4 determinism, §18)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

SCRIPT = textwrap.dedent("""
    import hashlib, json, sys
    from pathlib import Path
    from tokenbill.core.records import to_json
    from tokenbill.synth.fleet import generate

    out = Path(sys.argv[1])
    world = generate(seed=7, out_dir=out)
    h = hashlib.sha256()
    for key, path in sorted(world.source_files.items()):
        h.update(key.encode() + b"\\0" + hashlib.sha256(path.read_bytes()).digest())
    files = h.hexdigest()
    h = hashlib.sha256()
    for group in (world.sessions, world.requests, world.events, world.aggregates,
                  world.cost_lines, world.outcomes):
        for rec in group:
            h.update(json.dumps(to_json(rec), sort_keys=True).encode())
    h.update(repr(world.truth).encode())
    print(len(world.source_files), files, h.hexdigest(), world.today)
""")


def test_generate_is_byte_identical_across_processes(tmp_path: Path) -> None:
    procs = [subprocess.Popen([sys.executable, "-c", SCRIPT, str(tmp_path / f"run{i}")],
                              cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env={**os.environ, "PYTHONHASHSEED": str(i)})
             for i in (1, 2)]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=300)
        assert p.returncode == 0, err
        outs.append(out.strip())
    assert outs[0] == outs[1]
    n_files, *_rest = outs[0].split()
    assert int(n_files) > 50
