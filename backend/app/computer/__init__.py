"""Computer control: the AI loop that drives the real remote browser.

The package is split so that the parts worth testing hardest are the ones that
do not need a browser or an API key:

  prompt     the exact instructions the model is held to
  commands   strict JSON parsing and validation, pure functions
  controller the only code that touches the remote machine
  runner     the state machine: model -> command -> result -> screenshot
"""
