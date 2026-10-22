# Kokoro reference fixture

`kokoro_pipeline.py.txt` is the reference `kokoro/pipeline.py` from
https://github.com/hexgrad/kokoro/commit/6d87f4ae7abc2d14dbc4b3ef2e5f19852e861ac2
(only line endings/trailing whitespace normalized; Apache-2.0; license in `src/licenses/Kokoro-LICENSE.txt`).

Tests extract only KPipeline's class with AST and provide lightweight G2P/model
stand-ins. They compare our chunking and model-dependent setup without installing
PyTorch, loading a model, downloading assets, or contacting servers.
