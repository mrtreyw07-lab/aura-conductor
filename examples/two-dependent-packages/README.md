# Two dependent packages

The example behind `conductor demo`. Three packages extend a tiny Python project in `seed/`, with scripted agents,
so it runs the same everywhere and needs no API key.

| Package | Does | Depends on | Tier |
|---|---|---|---|
| A | adds `greeter/greeting.py`, its test, and a README line | | T0 |
| B | adds `greeter/cli.py`, which uses A's module, and its test | A | T0 |
| C | adds `greeter/farewell.py`, its test, and a README line in the same spot as A's | | T1 |

What the run shows:

- A and C start at once, each in its own worktree. B waits until A has landed, then starts from the tip that has it.
- A and C both append to the end of `README.md` from the same starting commit. Whichever lands second no longer
  squashes onto main, and the merge queue returns it as rework. Its next attempt starts from the new tip and lands.
  C is slower on purpose, so it is usually C.
- Half-way through B's implement stage the demo stops the run (the same stop request `conductor stop` writes). Every
  package holds where it is. Resume continues B's first attempt rather than starting a second one.
- Main ends with the seed commit and three landing commits, each reached by fast-forward.

To keep the repository and look around afterwards:

```bash
conductor demo --dir ../greeter-demo
cd ../greeter-demo/greeter
conductor status
conductor log
git log --oneline --graph --all
```

`policy.toml` is the allow-list the demo approves with. To run the workflow by hand in a repository built from
`seed/`, keep a copy of it outside that repository and pass `--policy`.
