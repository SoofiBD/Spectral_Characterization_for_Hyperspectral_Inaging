# GitHub repository settings

GitHub's repository-list subtitle is the **About / Description** field. It is not read from README
or this file; set it on GitHub after publishing. No remote repository is configured locally yet.

## Suggested name

`spectral-characterization`

## Description (copy as one line)

Desktop spectroscopy and hyperspectral imaging with Ocean HDX, Arduino monochromator control, raw data preservation, analysis and simulation.

## Topics

`spectroscopy`, `hyperspectral-imaging`, `spectrometer`, `ocean-insight`, `monochromator`,
`scientific-instrumentation`, `python`, `pyside6`, `data-acquisition`, `simulation`

## Publish from VS Code

1. Review Source Control changes and commit the application source, tests, `.github/` and documentation.
2. Use **Publish to GitHub** and choose the account, repository name and visibility.
3. On GitHub, edit the repository's About section: paste the description and add the topics above.
4. Check the first Actions run. Historical private-data checks are expected to skip without local measurements.

`data/`, `reports/`, `.venv/`, local outputs and SDK libraries remain excluded. The camera SDK header
and its license are included so fake-SDK tests can compile; real camera libraries must be installed locally.
A project-wide license has not been selected. No LICENSE was added or ownership assumptions made.
