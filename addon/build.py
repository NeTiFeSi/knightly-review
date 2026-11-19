"""Package the add-on as dist/knightly_review.ankiaddon (a zip of the package folder)."""

import pathlib
import zipfile

here = pathlib.Path(__file__).parent
src = here / "knightly_review"
out = here / "dist" / "knightly_review.ankiaddon"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            z.write(f, f.relative_to(src))
print(out)
