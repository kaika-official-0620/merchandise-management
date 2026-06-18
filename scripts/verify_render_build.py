from pathlib import Path
import py_compile

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


for relative_path in [
    "app.py",
    "render_app.py",
    "step3_vendor_workflow_patch.py",
]:
    py_compile.compile(str(ROOT / relative_path), doraise=True)
    print(f"py_compile ok: {relative_path}")


env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
for template_name in [
    "admin/vendor_documents_step3.html",
    "admin/documents_stepa_dashboard.html",
    "admin/vendor_mitsumori_create.html",
]:
    env.parse((ROOT / "templates" / template_name).read_text(encoding="utf-8"))
    print(f"jinja parse ok: {template_name}")
