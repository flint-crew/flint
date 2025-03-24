# Contributions

Contributions to `flint` are welcome and encouraged! The `flint-crew` are more than happy to help prototype ideas, give advice and engage in however we can afford to.

Should you wish to develop a new feature, add a new pipeline, or get familiar with the codebase here are a few developer pearls of wisdom.

## Install the developer tools

Do install the optional `flint` developer dependencies. These will help you identify issues earlier, and will help write consistently formatted and typed code.

Installing the developer dependencies should look something like this:

`pip install '.[dev]`

or

`pip install 'flint[dev]'

## Install and use `pre-commit`

`pre-commit` is a tool that runs whenever `git commit` has been issued. It will consider the code contributions and perform a series of checks and formatting changes. Some are straight forward (removing trailing white space), some are slightly more involved (running `mypy` or `ruff`).

The `pre-commit` hooks may be installed by running:

```bash
pre-commit install
```

when in the cloned `flint` repository. The next time you commit code a series of checks are automatically executed. On first execution this may take some time. The default settings will prohibit the commit should errors be detected. Some may be automatically fixed (e.g. code formatting), others will need to be manually inspected and resolved.

These `pre-commit` checks are also executed when submitting a pull request back into main on github.com. They will become a problem at some point if they are ignored.

*Please do ask* if you are unsure. The type system and `mypy` can be difficult to get used to, but once it clicks you will value your time investment.

## Dev Container

We now provide a [Dev Container](https://code.visualstudio.com/docs/devcontainers/containers) recipe and config. Support for this will depend on your machine and IDE of choice. We have tested this using Docker and Visual Studio Code. If using VSCode, simply install the [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) addon and run:

```prompt
> Dev Containers: Reopen in Container
```

## Type hints

Even though `python` is a dynamically typed language, `flint` makes extensive use of type hinting throughout its code base. This gives code analysis tools like `ruff` and `mypy` contextual information that helps to improve code quality and reduce bugs.

Functions throughout `flint` should all be typed, including all inputs and returns:

```python
from pathlib import Path


def my_function(arg1: str, arg2: int, arg3: int | float | None = None) -> Path:
    return Path("jack/Sparrow.fits")
```

Here all inputs are labelled with their intended input type. The return type is also noted. At run time these types are _not_ enforced - they are merely to help developers.

Should a function _not_ return anything then the return type should be `None`:

```python
def my_function(arg1: str, arg2: int, arg3: int | float | None = None) -> None:
    pass
```

## Functions return something

Try to have all functions return something, even if it is an input. Seems silly, but is often useful.

```python
def write_output(data: Any, output_path: Path) -> Path:
    with open(output_path, "w") as output_file:
        output_file.write(data)

    return output_path
```

## Specify keyword arguments everywhere

`Python` allows arguments to be pass by their name, even if they are positional. For example

```python
def bar(param1, param2, an_optional_parameter=3) -> None:
    return "JackSparrow"


bar(param2="Thisisparam2", param1="and Param1 is after param2", an_optional_parameter=2)
```

Note that although `param1` and `param2` are mandatory and positional arguments, we have been able to specify them by their name. Please do try to use this approach when using `flint` functions internally. It makes changes to the API a little more robust, and makes reading unfamiliar code easier to understand at a glance (i.e. more descriptive).

## Referencing paths

When attempting to handle a path-like string (e.g. for a file on disk) do using the `Path` object from the `pathlib` in the standard library. It will make your life a lot easier.

```python
from pathlib import Path

a = Path("some/other/path/jack_sparrow.fits")
a.name  # jack_sparrow.fits
a.parent  # "some/other/path

b = a.parent / "but/level"
b.mkdir(parent=True, exist_ok=True)  # make parent directories if need
```

## Docstrings

Do attempt to provide doc-strings for all functions. Should a function be sufficiently small and not intended for public consumption a short message may be placed as a string instead.

`flint-crew` have adopted the [Google python docstring (with types) style](https://sphinxcontrib-napoleon.readthedocs.io/en/latest/example_google.html). If you are developed with `vscode` we recommend the `autodocstring - Python Docstring Generator` tool to automatically template the docstring for you.

## Use the `BaseOptions` class

All (or most) of `flint`'s `Options` classes are derived from `flint.options.BaseOptions`. This uses `pydantic` in order to validate upon class initialisation that the provided values match the types they are listed as having.

If the values are unable to be coerced into a correct type than `pydantic` will raise an error.

All `flint` `Option` classes should be derived from `BaseOptions`:

```python
from flint.options import BaseOptions


class PirateOptions(BaseOptions):
    """An Options class used to create a new Pirate"""

    name: str
    """Name of the pirate we will create"""
    age: int = 34
    """The age, in years, of the pirate"""
    weaknesses: list[str] | None = None
    """Should the pirate have any weaknessess they go here"""
```

Should `PirateOptions(name="Jack Sparrow", age=34.23, weaknesses=None)` be invoked, the `age=34.23` input, which is a `float`, will be cast to an `int`.

Resulting classes are all _immutable_ by default and design. Should a value need to be updated use the `witH_options` method:

```python
new_pirate = existing_pirate.with_options(age=42)
assert new_pirate is not existing_pirage, "They are different instances"
```

## Function sizes and unit tests

`Flint` has a preference towards a procedural style of coding - although we do have classes we try to keep methods attached to them lite. This is a matter of preference, but so far it has been useful.

Try to keep functions to small discrete units of work. A function should do one thing. If it is going to be doing many things it should be calling multiple smaller functions. This makes it:

1 - easier to read
2 - logically separates the problem
3 - much easier to test and verify in isolation

Please do write tests that explicitly test the main code paths as best you can. This becomes a lot easier if the functions a sufficiently small. Refer to the existing set of tests for examples, but in a nutshell:

1 - a file that starts with `test` will be examined for tests
2 - a function that starts with `test_` will be called as a test

Tests may be run with

```bash
pytest [test_some_other_file.py]
```

Should the path to the file note be specified all tests across all files will be executed.

We currently do not have the facility to test code that passes through a container call.

## Naming variables, functions and other things

We live in a time not bound by character per line limits. Use descriptive names and do not skip a character or two to save space. Be verbose. Trust (or accept) that the code formatter will do things anyway.

Think of the future you being confused over the difference of `iidx` and `jiidx`.

Also, consider putting the type of the variable in the variable name, e.g. `input_file_path = Path(...)`.

## Indentation levels

If things become to indented then there is likely some logic that could be factored out into a separate function. For instance, deeply indented flow control in a loop could be refactored so that each loop is calling a function. This helps with developing robust tests.

## Loop end conditions

Should a loop be used do make sure that there is some terminating condition. The bbvious case is iterating over a list, where the `__next__` method indicates when the end of the list has been reached. For something like looping over until convergence has been reached (e.g. iterative sigma-clipping) always include an upper bound on how many times the loop may be executed.

## Use `assert` to make clear impossible conditions

`assert` statements are very useful to ensure some states can never be reached. They make the code more robust in that potential failure modes are not silently ignored. Don't be afraid of using them, and don't be afreaid of failing vocally. This is much preferred over 'maybe working but not sure'.
