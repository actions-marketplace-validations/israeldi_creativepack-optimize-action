# CreativePack HTML5 ZIP Optimizer

Optimize PNG/JPEG assets in a built HTML5 advertising ZIP, save a verified ZIP and JSON report, and fail the build if the complete ZIP remains over your byte budget.

Source is published in this repository. GitHub Marketplace publication is being prepared. The workflow below pins the independently reviewed implementation to an immutable source commit.

## Connect a build

1. Subscribe to [CreativePack on RapidAPI](https://rapidapi.com/IsraelDiego/api/creativepack). A free Basic plan is available; review the current request allowance and paid plans there.
2. Save your subscribed application's key as the repository secret `CREATIVEPACK_RAPIDAPI_KEY`.
3. Insert this step after your checkout and ZIP build steps. Use an Ubuntu runner with Python 3.10+ and Bash; no Python packages need installing.

```yaml
- name: Optimize the built creative ZIP
  id: creativepack
  uses: israeldi/creativepack-optimize-action@9dddbae7e6a4406af28fdfa29a998d49790f4183
  with:
    rapidapi-key: ${{ secrets.CREATIVEPACK_RAPIDAPI_KEY }}
    input: dist/banner.zip
    output: dist/banner-optimized.zip
    report: dist/creativepack-report.json
    budget-bytes: '153600'
    mode: lossless
    fail-on-budget: 'true'
```

The example target is 150 KiB for the **complete ZIP**. Choose the target your delivery requires. A budget result alone does not establish ad-network approval, functional behavior or visual acceptance. Review the optimized creative before shipping it.

For a first compatibility check, download the [owned synthetic ZIP](https://raw.githubusercontent.com/israeldi/flamingo-api-examples/main/sample-creative.zip), use it as the input, and set **`fail-on-budget: 'false'`**. You can use `budget-bytes: '4096'` to exercise reporting, but a valid `over_budget` result is expected whenever the processor cannot reach that target. Review the returned ZIP/report before enabling budget enforcement on your actual build. The [Python guide](https://flamingo-api-guides.flatemu1.chatgpt.site/html5-ad-zip-optimization/) explains the same service workflow. This repository's offline tests use a separate, smaller owned example archive and response from the public CreativePack specification.

## Inputs and results

| Input | Required/default | Meaning |
| --- | --- | --- |
| `rapidapi-key` | Required | Your subscribed CreativePack application key, supplied through a GitHub Actions secret. |
| `input` | Required | Existing ZIP relative to the checked-out workspace. |
| `output` | `creativepack-optimized.zip` | New output ZIP path in the workspace. |
| `report` | `creativepack-report.json` | New JSON report path in the workspace. |
| `budget-bytes` | Required | Complete ZIP target from 1,024 to 8,388,608 bytes. |
| `mode` | `lossless` | `lossless` or `balanced`. Balanced may change image colors; review it visually. |
| `fail-on-budget` | `true` | Fail after saving the verified result when it remains over budget. Use `false` for a reporting-only step. |

Input, output and report must be three different files. Output files must be new, and their parent directories must already exist. Absolute paths, traversal outside the workspace, leaf symlinks and control characters in paths are rejected. The original input is preserved.

Step outputs are `archive`, `report`, `status`, `input-bytes`, `output-bytes`, `saved-bytes` and `output-sha256`. `status` is `within_budget` or `over_budget` after a verified response. The output and report are saved before an over-budget failure, so a later artifact step using `if: always()` can collect them. Authentication, validation and transport failures produce no result files. A failure while writing GitHub's step-output file can occur after the verified ZIP/report were saved; inspect those files before choosing to rerun.

One Action invocation makes at most **one** processing request. There are no automatic retries or background calls. A completed `over_budget` request still uses the service allowance. Gateway failures may also count toward RapidAPI's request quota. Rerunning a job can consume another request.

## What is checked

The Action calls only `https://creativepack.p.rapidapi.com/v1/optimize` over verified HTTPS. The request is JSON containing the ZIP as canonical base64, `budget_bytes` and `mode`. There is no configurable provider origin or redirect following.

Before writing results, it checks the returned archive against its request-bound sizes and SHA-256 digests, verifies bounded ZIP integrity and member order/paths, validates the file manifest and confirms that nonimage bytes remain unchanged. It never extracts the ZIP or runs its HTML/JavaScript. Pixel equivalence, image dimensions and visual quality are processor responsibilities; this Action does not independently decode image codecs to certify them.

Supported local bounds are 8 MiB input ZIP, 256 members, 32 MiB expanded content and 16 MiB response JSON. Unsupported ZIP features or a result that increases the archive size are rejected. The service can preserve code, fonts, vectors and other nonimage assets while their size still prevents meeting your target.

## Data handling

The selected ZIP and processing options pass from the GitHub runner through RapidAPI to CreativePack. Use archives you are authorized to transmit. The key is placed only in the RapidAPI request header. It is not placed in command arguments, printed in diagnostics or included in step outputs. Error response content is not logged; an unexpected reflected key in a successful response is rejected before saving.

The Action writes the ZIP and report to your runner, and performs no artifact upload itself. The report can contain archive member names and processing metadata; choose artifact visibility and retention for your project. Read the service's current [privacy policy](https://creativepack.flamingoapis.synology.me:8443/privacy), [terms](https://creativepack.flamingoapis.synology.me:8443/terms) and [RapidAPI listing](https://rapidapi.com/IsraelDiego/api/creativepack) before connecting production creatives.

## Local verification

```sh
python3 -m unittest discover -s tests -v
```

The suite sends synthetic requests to a loopback HTTP fixture, while checking that production configuration selects the fixed RapidAPI hostname and a verifying TLS context. It covers the published example response, budget failure/report-only behavior, request encoding, credentials not appearing in diagnostics, invalid/reflected responses, size bounds, redirects, existing-file preservation and output collisions. It makes no real gateway processing requests and does not prove a live hosted GitHub Actions run or customer adoption.

## License

The standalone Action is MIT licensed; see `LICENSE`. The Action implementation is newly authored for this release from the public request/response contract. It does not copy or declare a license for the separate example Python client. The fixtures are the same owner's fictional CreativePack sample and example response.
