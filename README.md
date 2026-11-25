# code-test-lab

[Project usage guide](docs/usage.md) — installation, checks, sandbox workflow, and security gates.

## References

- [Repository](https://github.com/loxchi-s/code-test-lab)
- [Project usage guide](docs/usage.md)
- [GitHub Actions](https://docs.github.com/actions)
- [Docker](https://docs.docker.com/) — build only for a trusted checkout; for untrusted checkouts, use a disposable builder with no proxy credentials; if cleanup cannot be confirmed, return exit code `125` and inspect the original Docker daemon
- [Manual workflow prerequisites](https://docs.github.com/actions/managing-workflow-runs/manually-running-a-workflow): workflow must exist on the default branch; the branch selector chooses the workflow definition; Only public target repositories are accepted; test step has a 20-minute timeout
- [pytest](https://pytest.org/)
- [CodeRabbit AI Code Review](https://www.coderabbit.ai/)
- [CodeRabbit GitHub App installation](https://github.com/apps/coderabbit)
- [CodeRabbit documentation](https://docs.coderabbit.ai/)
