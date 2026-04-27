## `meson test` now accepts `--exclude`

`meson test` has a new `--exclude` argument to allow skipping named
tests. It takes a full test name and can be specified repeatedly. This
should help distributions that need to skip tests irrelevant for them
or known to be buggy.
