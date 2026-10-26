# Linux with NVIDIA Docker services

This path runs the inference services in Docker and the dashboard in ordinary Python. It requires
Python 3.11+, working Docker access and NVIDIA Container Toolkit on the host. Host drivers must support
the selected images. These helpers do not install Docker, drivers or system packages.

The commands, model mounts and service initialization were ported from a working server deployment.
The extracted runtime has offline tests, but this Linux-local path still needs target-machine testing.
Windows and Mac keep their existing launch paths; this is not Docker support for every GPU vendor.

## Configure

From the project root, copy the combined 24 GB example:

```bash
cp config/machines/examples/linux-nvidia-24gb.toml config/machines/my-machine.local.toml
```

Edit that file to choose the NVIDIA GPU for each instance (an index or GPU UUID). The example uses
one GPU with the existing 24 GB recipe: Whisper large-v3, Qwen3.8-27B and Qwen3-TTS 1.7B. It downloads
about 16.40 GB of model files, plus Docker images and the configured Qwen voice reference. File sizes
are not runtime memory estimates. Smaller recipes and explicit per-instance overrides can be used.
Machine files ending in `.local.toml` in this folder are ignored by Git.

Each `[api_servers.<service>.docker]` selects an image, internal port and optional entrypoint,
command, environment or restart policy. `[instances.<name>]` selects a service, GPU and optional
host/port/argument overrides. The supervisor reads defaults, the selected recipe's fragments and
this explicit machine file; it does not read desktop generated/local config layers.

Preview without downloading, contacting Docker or starting services:

```bash
PYTHON=python3 bash scripts/launch.sh --instances --config config/machines/my-machine.local.toml --show-commands
```

Relative paths are rooted at the project folder. Model files are downloaded into the usual `models/`
tree and individually mounted read-only. Existing model files and cached images are reused.
The example's image tags come from the working reference; pin a tested digest when reproducibility
matters. A moving tag does not cause an automatic pull/replacement on every supervisor run.

## Local execution

Start the services in one terminal:

```bash
PYTHON=python3 bash scripts/launch.sh --instances --config config/machines/my-machine.local.toml
```

The example uses Whisper 8081, llama 8082 and Qwen-TTS 8085, bound to loopback. Whisper and Qwen start
before llama; readiness includes configured voice initialization. Diagnostics go to the terminal,
model/image provisioning output to `logs/inference-supervisor/`, and container output to `docker logs`.

In a second terminal, start the app with the same file for its endpoints/voices:

```bash
python3 src/app.py config/machines/my-machine.local.toml
```

Open `http://127.0.0.1:8080/`. The app still uses its normal config layering; existing
`config_generated.toml` may contribute settings below the supplied machine file. If you change
instance endpoint overrides, keep the app's `[api_servers.<service>]` endpoints pointed at the
instances you want it to use. The combined example already matches them.

This terminal workflow supervises services only; it does not add Docker controls to the desktop
launcher. Stop the app separately with Ctrl+C. Ctrl+C/SIGTERM in the supervisor terminal stops the
containers it created or started for that run and keeps their stopped definitions for reuse.
It leaves already-running matching containers and independently running native endpoints alone.

Local mode defaults Docker's restart policy to `no`; the active supervisor handles recovery. An
explicit `docker.restart` value overrides that default. Changing a stopped owned container's settings
recreates it; local mode will not replace a running container it did not start. Stop that container
explicitly before changing its configuration. Unowned containers are never replaced or stopped.

A supervisor crash can leave containers running. A later launch can reuse them. To stop configured
owned containers explicitly, first stop any supervising process, then run:

```bash
python3 src/inference_instances.py --config config/machines/my-machine.local.toml --stop
```

## Shared server behavior

The same runtime exposes `--mode server`: Docker containers persist after supervisor exit, and
their default restart policy is `unless-stopped`. Native children owned by the supervisor still stop
on exit in either mode. Each instance has its own retry/readiness state, and `after` controls initial
startup order without cascading runtime restarts to other services. A private host wrapper can reuse
this runtime; no proxy routing, registration, network tunnel or host heartbeat is implemented here.

Single-engine Docker examples and a two-GPU native example are also in `config/machines/examples/`.
Native examples require separately provisioned Linux executables/models; the Docker path provisions
its images/models itself. Ownership labels and container names identify this runtime's containers;
another host wrapper can retain its own existing label keys and name prefix when integrating later.
