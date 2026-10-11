"""Guarded local CCR 3.x fix: terminate an already-started downstream on body error.

The unpatched Fastify bridge leaves a client waiting after upstream resets.
Always run the isolated HTTP regression after applying. Recheck after CCR upgrades.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

OLD = 'for(let we of uq)we.on("error",vAe);'
NEW = (
    'for(let we of uq)we.on("error",err=>{vAe(err);Ym.unpipe(r);r.destroy();Hde(uq)});'
)


def repair(target: Path, *, apply=False):
    source = target.read_text()
    if NEW in source:
        return {"state": "already_patched"}
    if source.count(OLD) != 1:
        raise RuntimeError(
            "Unsupported CCR bundle; expected bridge not uniquely found."
        )
    digest = hashlib.sha256(source.encode()).hexdigest()
    if not apply:
        return {"state": "patch_available", "sha256": digest}
    backup = target.with_name(target.name + ".before-rag-stream-fix-" + digest[:12])
    if not backup.exists():
        shutil.copy2(target, backup)
    candidate = target.with_suffix(".rag-stream-fix.tmp")
    candidate.write_text(source.replace(OLD, NEW))
    shutil.copymode(target, candidate)
    candidate.replace(target)
    return {"state": "patched", "backup": str(backup), "sha256_before": digest}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(repair(args.target, apply=args.apply))
