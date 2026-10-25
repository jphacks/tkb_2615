"""Compatibility entry point for the stronger coordinate-and-gait gameplay verifier.

The former pixel-change test could pass on breathing alone. This entry now verifies
real recorded translation, exact playback rates, buffered time and shared A/B clocks.
Output is results.json plus consecutive screenshots; --record adds an actual MP4.
"""

from review_demo_v2_gameplay import main

if __name__ == "__main__":
    main()
