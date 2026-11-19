# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Entry point for `python -m gitlytics`."""

from gitlytics.cli import main

# Delegate directly to the CLI entrypoint when invoked as a module.
if __name__ == "__main__":
    main()
