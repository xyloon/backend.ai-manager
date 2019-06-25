Backend.AI Manager with API Gateway
===================================

Package Structure
-----------------

* `ai.backend`
  - `manager`: Abstraction of agents and computation kernels
  - `gateway`: User and Admin API (REST/GraphQL) gateway based on aiohttp

Installation
------------

Please visit [the installation guides](https://github.com/lablup/backend.ai/wiki).

### For development

#### Prerequisites

* `libnsappy-dev` or `snappy-devel` system package depending on your distro
* Python 3.6 or higher with [pyenv](https://github.com/pyenv/pyenv)
and [pyenv-virtualenv](https://github.com/pyenv/pyenv-virtualenv) (optional but recommneded)
* Docker 18.03 or later with docker-compose (18.09 or later is recommended)

Clone [the meta repository](https://github.com/lablup/backend.ai) and install a "halfstack"
configuration.  The halfstack configuration installs and runs several dependency daemons such as etcd in
the background.

```console
$ git clone https://github.com/lablup/backend.ai halfstack
$ cd halfstack
$ docker-compose -f docker-compose.halfstack.yml up -d
```

Then prepare the source clone of the agent as follows.
First install the current working copy.

```console
$ git clone https://github.com/lablup/backend.ai-manager manager
$ cd manager
$ pyenv virtualenv venv-manager
$ pyenv local venv-manager
$ pip install -U pip setuptools
$ pip install -U -r requirements-dev.txt
```

From now on, let's assume all shell commands are executed inside the virtualenv.

### Halfstack (single-node development & testing)

First install it:
```console
$ cp config/halfstack.toml ./manager.toml
$ cp config/halfstack.alembic.ini alembic.ini
```

Set up Redis:
```console
$ python -m ai.backend.manager.cli etcd put config/redis/addr 127.0.0.1:8120
```

Set up the public Docker registry:
```console
$ python -m ai.backend.manager.cli etcd put config/docker/registry/index.docker.io "https://registry-1.docker.io"
$ python -m ai.backend.manager.cli etcd put config/docker/registry/index.docker.io/username "lablup"
$ python -m ai.backend.manager.cli rescan-images
```

Set up the vfolder paths:
```console
$ mkdir -p "$HOME/vfroot/local"
$ python -m ai.backend.manager.cli etcd put vfolder/_mount "$HOME/vfroot"
$ python -m ai.backend.manager.cli etcd put vfolder/_default_host local
```

Set up the database:
```console
$ python -m ai.backend.manager.cli schema oneshot
$ python -m ai.backend.manager.cli fixture populate sample-configs/example-keypairs.json
$ python -m ai.backend.manager.cli fixture populate sample-configs/example-resource_presets.json
```

Then run it (for debugging, append a "--debug" flag):

```console
$ python -m ai.backend.gateway.server
```

To run tests:

```console
$ python -m flake8 src tests
$ python -m pytest -m 'not integration' tests
```

To run tests including integration tests, you first need to install the agent in the same virtualenv.
Please refer [the README of Backend.AI Agent](https://github.com/lablup/backend.ai-agent) for
installation instructions, while keeping the same virtualenv.

## Deployment

### Configuration

Put a TOML-formatted manager configuration (see the sample in `config/sample.toml`)
in one of the following locations:

 * `manager.toml` (current working directory)
 * `~/.config/backend.ai/manager.toml` (user-config directory)
 * `/etc/backend.ai/manager.toml` (system-config directory)

Only the first found one is used by the daemon.

### Running from a command line

The minimal command to execute:

```sh
python -m ai.backend.gateway.server
```

For more arguments and options, run the command with `--help` option.
