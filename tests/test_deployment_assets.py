import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_BUNDLED_ASSETS = {
    "apps/momentum-factor": ["frontend/js/data/prices.json"],
    "apps/factor-regression": [
        "frontend/js/data/catalog.json",
        "frontend/js/data/factors.json",
        "frontend/js/data/industries.json",
    ],
}


class DeploymentAssetTests(unittest.TestCase):
    def test_bundled_frontend_data_is_tracked_for_clean_image_builds(self):
        missing = []

        for submodule, assets in REQUIRED_BUNDLED_ASSETS.items():
            submodule_path = REPO_ROOT / submodule
            for asset in assets:
                result = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", asset],
                    cwd=submodule_path,
                    capture_output=True,
                    check=False,
                    text=True,
                )
                if result.returncode != 0:
                    missing.append(f"{submodule}/{asset}")

        self.assertEqual(
            missing,
            [],
            "Bundled data missing from clean Git checkouts: " + ", ".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
