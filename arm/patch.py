"""Install the tv callbacks at ARM's mounted/ripped lifecycle points.

ARM does not currently expose a supported plugin hook for these events.  The
patch is deliberately exact and makes the image build fail when upstream ARM
changes the surrounding code, instead of silently capturing at the wrong
time.  Update the anchors after reviewing a new ARM release.
"""

from pathlib import Path


ARM_MAIN = Path("/opt/arm/arm/ripper/main.py")

IDENTIFY_ANCHOR = "    identify.identify(job)\n"
IDENTIFY_REPLACEMENT = IDENTIFY_ANCHOR + """
    # Local integration: capture navigation data while the disc is mounted.
    try:
        from bridge import capture_for_arm
        capture_for_arm(job)
    except Exception:
        logging.exception("TV disc capture failed")
"""

RIP_ANCHOR = (
    "        arm_ripper.rip_visual_media(have_dupes, job, log_file, "
    "job.has_track_99)\n"
)
RIP_REPLACEMENT = RIP_ANCHOR + """
        # Local integration: enqueue the completed rip before ARM ejects.
        try:
            from bridge import complete_for_arm
            complete_for_arm(job)
        except Exception:
            logging.exception("TV rip handoff failed")
"""


def replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    count = source.count(anchor)
    if count != 1:
        raise RuntimeError(
            f"Cannot patch ARM {label}: expected one exact anchor, found {count}. "
            "Review arm/patch.py against the selected ARM image."
        )
    return source.replace(anchor, replacement, 1)


def main() -> None:
    source = ARM_MAIN.read_text(encoding="utf-8")
    if "from bridge import capture_for_arm" in source:
        raise RuntimeError("ARM main.py is already patched")
    source = replace_once(source, IDENTIFY_ANCHOR, IDENTIFY_REPLACEMENT, "identify hook")
    source = replace_once(source, RIP_ANCHOR, RIP_REPLACEMENT, "rip hook")
    ARM_MAIN.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()

