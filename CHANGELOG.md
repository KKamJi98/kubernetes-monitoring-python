# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

### Refactor

* Rename '재시작된 컨테이너 확인 및 로그 조회' menu to 'Container Monitoring (재시작된 컨테이너 및 로그)' for consistency.

## [1.5.0](https://github.com/KKamJi98/kubernetes-monitoring-python/compare/v1.4.0...v1.5.0) (2025-10-21)


### Features

* **cli:** add live command input prompt ([84aff03](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/84aff03b7f997eddf413d2d57af5548c8f45cb6b))
* **context:** auto-reload on kubeconfig change ([ddc6ea4](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/ddc6ea47eca10e2d2f19f0bffe698d3fc6b02ea6))
* **export:** add csv export option ([3c04096](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/3c040965045c05410af4fe90d5789ba7a62584d9))
* **filter:** support choosing node label keys ([786bf5d](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/786bf5d3292f13234dd8aef62008174c52e2a00b))
* **ui:** annotate time headers with timezone ([5cea6f2](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/5cea6f2fd82b39c0511061eb156d5f36489496d4))
* **ui:** display timestamps in kst timezone ([252b3ce](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/252b3ce5b1dee794fc196989c9a1abdf586afe22))
* **ui:** handle terminal resize events ([fe57837](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/fe578371d136efedd79ea5ba9968433c9b36ab79))
* **ui:** improve nodegroup filtering and readiness ([5628496](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/56284966722554a57f7fc25d837fb4b1ecc3e976))
* **ui:** refine snapshot export and ready display ([05d3e84](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/05d3e840c43a14a0cabaa05df07e06747d74d56b))
* **ui:** simplify live output and snapshot format ([1b05f52](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/1b05f52322023ee348ef65a0c8c0a1b8b62a86e3))


### Bug Fixes

* **attrdict:** guard integer lookups from raising ([3361c06](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/3361c06950d7485104994db1ddac6b8d87445510))
* **ci:** resolve mypy and pytest errors ([941069d](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/941069de0894b1ebbb1a6128103be5eb43bd4bf4))
* **ci:** resolve mypy and pytest errors ([79ead3f](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/79ead3f4fae066f3f6f6d7b00ad641760878ce2e))
* **cli:** add timeouts for pod monitor requests ([049ac99](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/049ac99e54e9716ad07c70b11fc2416e2e0d837e))
* **cli:** improve live input responsiveness ([053ffb7](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/053ffb766204d23b758a4d615eea9ce6df66c608))
* **context:** remove non-existent cleanup_and_reset call ([61c9d91](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/61c9d9183539cab98c1865aedc42b7db2a4440e8))
* **node:** handle non-string label keys safely ([b8fa676](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/b8fa676a3dfb918b1fcd4feeac74f74023edddfb))
* **node:** resolve created-at parsing and reduce timeouts ([a3b6884](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/a3b68842b753c42787c3becf12babd0eb69f25dd))
* **node:** show zone values and allow label-only selection ([e1792f6](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/e1792f68b7e39da36e548a326a0774b703749e19))
* **node:** skip invalid role labels ([355820a](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/355820a1142738834c9d8fd0178c060d3c1e0385))
* **pod-monitor:** balance restart and creation ordering ([fcb1018](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/fcb1018669947957f43f8d971a6b62f2dc55f75f))
* **pod-monitor:** prioritize restarted pods in creation view ([9229283](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/922928323045aa2c108753618cb2c554e404793d))
* **types:** normalize node zone label lookup ([2101289](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/21012899a4c0a681bce1465456b856e02c5e70d0))
* **ui:** sync node label headers and menu text ([827c34e](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/827c34e3d478bb388b81ecf6b51a30d71d4f665c))
* **ui:** wrap long resource names in live tables ([44aefc0](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/44aefc0f181429745b3e97d5ba476a6c3f2b8c3e))


### Performance Improvements

* **kubectl:** cache selector lookups and reuse node data ([f3be584](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/f3be584df817385a0b5593f7bd94c5b6d9493be9))
* **pods:** cache kubectl responses and document tuning ([79b3df0](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/79b3df00c1dc492654fd3f8cc6dc952222a1b385))


### Documentation

* remove AGENTS.md ([b0053dc](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/b0053dc9775b679369789cc0dafbc3d0dff1ed54))

## [1.4.0](https://github.com/KKamJi98/kubernetes-monitoring-python/compare/v1.3.0...v1.4.0) (2025-10-03)


### Features

* add graceful CLI shutdown handlers ([9f07076](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/9f07076ae112c3e91d72434cd6ef5f813362ffa5))
* support kubecolor for kubectl commands ([0eabe89](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/0eabe8999ac0bc3396d2982113349ef170af98c6))


### Bug Fixes

* add leading blank line before graceful-exit messages ([c62fff4](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/c62fff4ad0832f7a4b5b2bac9aa0a87f0f5d542d))
* treat Succeeded pods as normal in counts ([4efc9b7](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/4efc9b72764f1beed32da4c31cac8c8fc2868777))


### Documentation

* switch formatting to Ruff and update quality rules ([c062fd6](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/c062fd6bd53b4264c11d80ba550017ba60603166))
* update README for Ruff and dev workflow ([a1aa14a](https://github.com/KKamJi98/kubernetes-monitoring-python/commit/a1aa14a5801948048903cdb88f5a8426a05c1fdc))

## [1.3.0](https://github.com/KKamJi98/monitoring-kubernetes/compare/v1.2.0...v1.3.0) (2025-07-04)


### Features

* Apply consistent styling to menu indices ([6c63964](https://github.com/KKamJi98/monitoring-kubernetes/commit/6c63964d9f0900e807b46c9cb65793d86fdef4eb))
* Combine error-related features and update docs ([8c0c7b3](https://github.com/KKamJi98/monitoring-kubernetes/commit/8c0c7b3e621b7afa59ccdf8ea647c16236bf996a))
* Update CI/CD, linting, and type checking configurations ([b95aa0d](https://github.com/KKamJi98/monitoring-kubernetes/commit/b95aa0d6343c3dbed0dab95cef10e64c7ec05fe0))


### Bug Fixes

* Add debug steps to CI/CD pipeline for PATH and venv contents ([43faff5](https://github.com/KKamJi98/monitoring-kubernetes/commit/43faff570a6e9ce5ea99d45a3591ebc3db494284))
* Add virtual environment to PATH in CI/CD pipeline ([abef8aa](https://github.com/KKamJi98/monitoring-kubernetes/commit/abef8aafaa41d3236da33cac0aabe1b76da9f3bb))
* Fix CI/CD pipeline to activate virtual environment ([0896d00](https://github.com/KKamJi98/monitoring-kubernetes/commit/0896d000deb8430b491aeafa25ad56cefe2a4554))
* Resolve CI pipeline issues ([f110244](https://github.com/KKamJi98/monitoring-kubernetes/commit/f11024486b9ccec6183a8275922dc2686d2c2cf5))


### Documentation

* Remove GEMINI.md from remote and ignore locally ([cba197e](https://github.com/KKamJi98/monitoring-kubernetes/commit/cba197eb63bb7ccf59d15216bbd4a2999dc48e70))

## [1.3.0](https://github.com/KKamJi98/monitoring-kubernetes/compare/v1.2.0...v1.3.0) (2025-07-04)


### Refactor

* Combine 'Error Pod Catch' and 'Error Log Catch' features into a single, streamlined function.


### Documentation

* Update README.md to reflect the combined functionality and re-ordered menu.

## [1.2.0](https://github.com/KKamJi98/monitoring-kubernetes/compare/v1.1.0...v1.2.0) (2025-06-28)


### Features

* Apply rich library for enhanced main menu display ([4d821c0](https://github.com/KKamJi98/monitoring-kubernetes/commit/4d821c064accc1c09a08eb5b11cd5e4dc9bc1bf1))
* Enhance main menu display ([76d2179](https://github.com/KKamJi98/monitoring-kubernetes/commit/76d2179c82e334ab84d7d472df1c8b01a733c4dc))


### Bug Fixes

* Ensure consistent rich styling for all main menu items ([7596ae2](https://github.com/KKamJi98/monitoring-kubernetes/commit/7596ae29685893c897266f9f4868f8fb310a9ee6))


### Documentation

* Update README.md UI and refactor menu title ([8c179b1](https://github.com/KKamJi98/monitoring-kubernetes/commit/8c179b13ce165658278f63d780db34427b773390))

## [1.1.0](https://github.com/KKamJi98/monitoring-kubernetes/compare/v1.0.1...v1.1.0) (2025-06-28)


### Features

* Add CI/CD pipeline and migrate from Poetry to UV ([30cc5c6](https://github.com/KKamJi98/monitoring-kubernetes/commit/30cc5c638dbbf6a704ee6009f94d888fa207ecd1))
* Integrate CI workflows and add GEMINI.md ([b7ab113](https://github.com/KKamJi98/monitoring-kubernetes/commit/b7ab113b97781324b94d75f3b7512e1a3aa1d432))


### Bug Fixes

* Add virtual environment creation to CI workflow ([b8dde70](https://github.com/KKamJi98/monitoring-kubernetes/commit/b8dde70b1406989c25b5ad2a53e4ef817b4e60e6))
* Adjust pyproject.toml dependencies to PEP 621 format ([5dcf80c](https://github.com/KKamJi98/monitoring-kubernetes/commit/5dcf80cb49fdfddf0ef4186f35b4e7f981070804))
* Correct pyproject.toml dependencies format for uv ([1b40125](https://github.com/KKamJi98/monitoring-kubernetes/commit/1b401254b651f800e31b962f70b96644ef81a953))


### Documentation

* Add GEMINI.md to .gitignore ([874f894](https://github.com/KKamJi98/monitoring-kubernetes/commit/874f894bfcda46ec93737645928d7892757621ba))

## [Unreleased]

### Added

- Export Live snapshots as code-block markdown alongside same-name CSV files for external sharing.
- Output container Ready ratios in bracketed form to avoid spreadsheet auto-formatting issues.

### Changed

- Remove the kubectl command footer from Live views and markdown snapshots to reduce on-screen clutter.
- Bump Kubernetes client to v34.1.0 and refresh supporting dependencies (requests 2.32.5, rich 14.1.0, PyYAML 6.0.3).
- Raise the minimum supported Python version to 3.9 and align lint/type-check targets.

## [0.1.0] - 2025-06-29
### Added
- Initial project setup.
