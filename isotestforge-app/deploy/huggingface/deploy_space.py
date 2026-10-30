"""Crée (si besoin) un Space Docker Hugging Face et y publie ISO-TestForge.

Variables : HF_TOKEN (jeton « write »), HF_SPACE (ex. utilisateur/isotestforge, facultatif),
ITF_BASIC_AUTH (facultatif, enregistré comme secret du Space), HF_SPACE_PRIVATE (« true » par défaut).
"""
import os
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[2]
HEADER = """---
title: ISO-TestForge
emoji: 💳
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 8080
pinned: false
---

"""
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "workflows", "itf.sqlite", "generated", "tests")

api = HfApi(token=os.environ["HF_TOKEN"])
space = os.environ.get("HF_SPACE") or f"{api.whoami()['name']}/isotestforge"
private = os.environ.get("HF_SPACE_PRIVATE", "true").lower() != "false"

api.create_repo(space, repo_type="space", space_sdk="docker", private=private, exist_ok=True)
variables = api.get_space_variables(space)
for key in ("ITF_BASIC_AUTH", "ANTHROPIC_API_KEY"):
    if os.environ.get(key):
        # Hugging Face refuse de démarrer si une variable et un secret portent le même nom
        if key in variables:
            api.delete_space_variable(space, key)
        api.add_space_secret(space, key, os.environ[key])

with tempfile.TemporaryDirectory() as tmp:
    dest = Path(tmp) / "space"
    shutil.copytree(ROOT, dest, ignore=IGNORE)
    (dest / "README.md").write_text(HEADER + (ROOT / "README.md").read_text(encoding="utf-8"), encoding="utf-8")
    api.upload_folder(folder_path=str(dest), repo_id=space, repo_type="space",
                      commit_message="Déploiement ISO-TestForge", delete_patterns=["*"])

user, name = space.split("/")
print(f"Space : https://huggingface.co/spaces/{space}")
print(f"Application : https://{user.lower()}-{name.lower().replace('_', '-')}.hf.space")
