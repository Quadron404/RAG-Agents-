"""Run every check in this package.

    python -m tests.run

The tests are plain scripts that raise SystemExit, not pytest functions, so they
can be run individually while debugging. This runner just runs them all and
summarises, because the point of the suite is to be run before every deploy.
"""
import subprocess
import sys

TESTS = (
    "test_ui_served",
    "test_screen_url",
    "test_screen_isolation",
    "test_tunnel_url",
    "test_websockify_proxy",
    "test_vnc_bridge",
    "test_rfb_handshake",
    # Computer control.  Run before test_bind_mutations because it is the only
    # check that would notice a command allowlist quietly growing, and it needs
    # to pass for a feature that can click a user's real browser to be
    # deployable at all.
    "test_computer_control",
    # The `next_step` memory, the second half of the model's memory of a run.
    # Paired with the check above on purpose: `history` says what the model just
    # did and this says what it meant to do next, so a run can only be trusted to
    # progress rather than to repeat itself if both are proven to arrive, be
    # replaced by the newest value, and reach the executor as neither.  It sits
    # next to test_computer_control rather than with the provider checks because it
    # drives none of them: the timing it proves is the loop's, not a provider's.
    "test_computer_next_step",
    # The UI map: what the model is shown, the id it picks, the point that runs.
    # Paired with the check above on purpose: `history` and `next_step` are the
    # model's memory of a run and this is its evidence about the page -- the
    # boxes and ids that turn "click the button" into a named control instead
    # of a guessed coordinate.  It drives the loop itself with a fake machine
    # whose `uimap` answers like the agent's, so the map's journey (request,
    # fresh read, refusal, resolved point) is proved end to end, not just in
    # the pure functions.
    "test_computer_ui_map",
    # The real xdotool layer on the agent.  Paired with the check above on
    # purpose: that one proves the loop only asks for allowed commands, and this
    # one proves the agent cannot be talked into running anything else.  Either
    # half alone would leave a way through.
    "test_computer_input",
    # The screenshot itself when the browser is real but the X display is not.
    # Paired with the check above on purpose: that one proves a model's
    # coordinates can never become a shell command, this one proves the loop can
    # still see the page those coordinates are aimed at -- the one thing a
    # control loop cannot fake, and the reason the CDP capture exists at all.
    "test_computer_cdp_capture",
    # The second computer-control provider.  Paired with the check above: that
    # one proves the loop drives a real machine through the OpenAI-compatible
    # adapter, this one proves swapping in Mistral changed who answers and
    # nothing else -- same prompt, same history, same screenshot, same parser,
    # same executors.  A second provider is exactly where two implementations of
    # a control loop start to drift.
    "test_computer_providers",
    # Groq, the third.  Same claim as the two above with one provider further
    # along: a new provider is a name, a key and a model, and nothing about the
    # loop, the prompt, the strict JSON protocol or the screenshot changes.  Also
    # the only place that proves GROQ_API_KEY never reaches the browser.
    "test_computer_groq",
    # Provider failure handling.  Paired with the two above because they only
    # ever drive a provider that answers: a 429, a 5xx, an unparseable body and
    # a refused connection are the paths where the loop's own error reporting
    # runs, and it used to raise a second exception from inside its handler, so
    # the provider's real reason was replaced by a NameError.
    "test_computer_provider_failures",
    # The same failures over a real socket.  The check above substitutes a
    # provider that raises, which cannot prove that a streamed error body is
    # actually read, that Retry-After survives the round trip, or that a refused
    # connection is not reported as "provider reached".
    "test_provider_http_429",
    # The supervisor script.  Before test_bind_mutations, which edits files:
    # this one reads them, and it has to pass before anything is deployed on a
    # machine whose only screen depends on supervise.sh starting.
    "test_supervisor",
    # Why the Codespace came up with no URL: install.sh created /tmp/ragdesktop
    # as root, so every pid write from the unprivileged stack failed with EACCES
    # and the tunnel was never asked for a hostname.
    "test_run_dir_ownership",
    # Runs last: it edits files to prove the isolation checks actually fail when
    # a listener is opened to a wildcard, and restores them afterwards.
    "test_bind_mutations",
)


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    for name in TESTS:
        proc = subprocess.run(
            [sys.executable, "-m", f"tests.{name}"],
            capture_output=True,
            text=True,
        )
        results.append((name, proc.returncode == 0, (proc.stdout or proc.stderr).strip()))

    width = max(len(name) for name, _, _ in results)
    for name, ok, _ in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}")

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print(f"{len(failed)} of {len(results)} failed:")
        for name, ok, out in results:
            if not ok:
                print(f"\n--- {name} ---")
                print(out)
        return 1

    print(f"all {len(results)} checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
