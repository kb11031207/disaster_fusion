"""
Quick MP4 atom walker — extract duration from moov/mvhd before we
ship a half-gig file to S3 only to have Pegasus reject it for length.

Usage:
    python scripts/m2_inspect_mp4.py "<path to mp4>"
"""

import struct
import sys
from pathlib import Path


def _read_atom_header(fp):
    header = fp.read(8)
    if len(header) < 8:
        return None, None
    size, kind = struct.unpack(">I4s", header)
    return size, kind.decode("ascii", errors="replace")


def find_mvhd(path: Path) -> dict:
    """Walk top-level atoms, descend into moov, read mvhd duration."""
    with path.open("rb") as fp:
        file_size = path.stat().st_size
        offset = 0
        while offset < file_size:
            fp.seek(offset)
            size, kind = _read_atom_header(fp)
            if size is None:
                break
            if kind == "moov":
                # moov is a container — walk its children for mvhd.
                moov_end = offset + size
                inner = offset + 8
                while inner < moov_end:
                    fp.seek(inner)
                    isize, ikind = _read_atom_header(fp)
                    if isize is None:
                        break
                    if ikind == "mvhd":
                        # mvhd v0: 1 byte version + 3 flags + 4 created
                        # + 4 modified + 4 timescale + 4 duration ...
                        # mvhd v1: same start, then 8 created/modified +
                        # 4 timescale + 8 duration.
                        fp.seek(inner + 8)
                        version = fp.read(1)[0]
                        fp.read(3)  # flags
                        if version == 1:
                            fp.read(16)  # created+modified (64-bit)
                            timescale = struct.unpack(">I", fp.read(4))[0]
                            duration = struct.unpack(">Q", fp.read(8))[0]
                        else:
                            fp.read(8)  # created+modified (32-bit)
                            timescale = struct.unpack(">I", fp.read(4))[0]
                            duration = struct.unpack(">I", fp.read(4))[0]
                        seconds = duration / timescale if timescale else 0
                        return {
                            "found": True,
                            "timescale": timescale,
                            "duration_units": duration,
                            "duration_seconds": seconds,
                            "duration_minutes": seconds / 60.0,
                            "version": version,
                        }
                    inner += isize
                return {"found": False, "reason": "moov has no mvhd"}
            offset += size
    return {"found": False, "reason": "no moov atom in file"}


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: m2_inspect_mp4.py <path>")
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"Not a file: {path}")
        return 2
    print(f"File:  {path.name}")
    print(f"Size:  {path.stat().st_size:,} bytes "
          f"({path.stat().st_size / 1_000_000:.1f} MB)")
    info = find_mvhd(path)
    if not info["found"]:
        print(f"mvhd:  NOT FOUND ({info['reason']})")
        return 1
    print(f"mvhd v{info['version']}:")
    print(f"  timescale:        {info['timescale']}")
    print(f"  duration_units:   {info['duration_units']}")
    print(f"  duration_seconds: {info['duration_seconds']:.2f}")
    print(f"  duration_minutes: {info['duration_minutes']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
