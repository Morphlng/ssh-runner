# Release checklist

The initial public version is 0.1.0. Local preparation does not publish a package or create a GitHub release.

1. Review `__version__`, README links, license and package metadata. Record changes in GitHub Release notes; no separate change log is maintained.
2. Run the macOS/Linux CI matrix including Python 3.8, formatting, full localhost integration tests, wheel/sdist validation and installed-resource tests.
3. Run `gitleaks dir .` for the working tree and `gitleaks git .` for history. The local private root config is ignored; never add it to Git or package artifacts. Review all files selected for commit and both archives.
4. Enable GitHub private vulnerability reporting and appropriate branch/environment protections.
5. Configure TestPyPI/PyPI Trusted Publishers for `Morphlng/ssh-runner`, workflow `publish.yml`, and environments `testpypi` / `pypi`. Add required reviewers to both environments.
6. Dispatch the publish workflow to `testpypi`. It only builds from `main`; check the version does not already exist. Install the exact version from TestPyPI in a disposable environment and repeat CLI/config/skill smoke checks.
7. After approval, create/review a `v0.1.0` release tag from the verified main commit, and dispatch the workflow at that tag to `pypi`. Its version/tag guard must pass. Verify installation from PyPI, then create the GitHub release with its change notes and point onboarding to published installation.

The manual publish workflow does not create a tag, GitHub release or external account configuration. Never use long-lived publishing tokens when Trusted Publishing is available.

Publishing follows the [PyPI Trusted Publishing guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/); action updates are managed through Dependabot.
