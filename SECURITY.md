# Security policy

## CoDoc runs code

CoDoc builds and executes code on the machine it runs on:

- the repository's own tests (`baseline`, `docgen`, `testrun`);
- code and test suites **written by an LLM** (`docgen` regenerates source files from their docs;
  `testgen` writes and runs new tests), including mutated versions of your code;
- build commands you pass, such as `--build-cmd` and `--test-cmd`.

The sandbox only keeps those runs from writing into the analysed repository. It does **not**
isolate them from the rest of your machine, the network or your credentials. Generated code can
do anything the repository's own code could do.

Recommendations:

- Run CoDoc on code you do not fully trust only inside a container or virtual machine, with no
  access to secrets other than the LLM API key.
- Use an API key with a spending limit, and keep it in the environment or a local `.env` file
  that is never committed.
- Prompts include source code. Send proprietary code only to an endpoint you are allowed to
  share it with (`OPENAI_BASE_URL` can point at a self-hosted OpenAI-compatible server).
- Review `OUT/` before publishing it: it holds prompts, replies and copies of the analysed code.

## Supported versions

Security fixes go into the latest released version.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/ccsnow127/CoDoc/security/advisories/new)
("Report a vulnerability" on the repository's Security tab). Do not open a public issue.
Include the version (`codocs --version`), what you ran and what an attacker could achieve.
You should get a first answer within a week.
