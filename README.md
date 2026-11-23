# Anna’s Archive Calibre Store

Search Anna’s Archive from Calibre’s **Get books** window. The plugin supports language, format, content type, source, access method, and sorting filters. It handles table and list results, skips incomplete records, and deduplicates pages by record ID.

## Compatibility and installation

The plugin targets Calibre 5.0 or later on Windows, macOS, and Linux. The installed ZIP is exercised with offline fixtures in Calibre 5.0 and 9.19 on Linux. CI also runs the native Calibre 9.19 smoke test on Windows and macOS.

1. Build the plugin ZIP below, or download the `calibre-annas-archive-plugin` artifact from the **Artifacts** section of a [GitHub Actions run](https://github.com/tubbygregory/calibre_annas_archive/actions). If GitHub wraps the artifact in another ZIP, extract it and use the inner `calibre_annas_archive-v<VERSION>.zip`.
2. In Calibre, go to **Preferences → Plugins → Load plugin from file** and select the plugin ZIP.
3. Restart Calibre. Open **Get books** and enable **Anna’s Archive** in the list of stores.

## Configuration

Open **Preferences → Plugins → Store → Anna’s Archive → Customize plugin**.

- **Search** lets you select website filters and the sort order. An empty filter group leaves that field unrestricted. **Prefer EPUB within each search page** moves EPUB results ahead of other records on the same page while retaining the site’s order within each group. Search metadata is kept in memory for 300 seconds by default. Set **Search cache expiry** to zero to disable it.
- **Mirrors** lets you add, remove, and move public HTTP(S) addresses. Searches try the list in order and continue pagination on the mirror that served the last page while it remains usable. Failed mirrors cool down for two minutes or the server’s longer `Retry-After` value. **Test selected mirror** reports whether it can serve a valid search page and runs in the background. **Open in browser** lets you check the same mirror yourself. **Reset defaults** restores the bundled addresses. The unchanged list of defaults from version 0.2.4 migrates automatically. Other valid saved lists retain their order, including an intentionally empty list.
- **Downloads** keeps the existing extension and Content-Type preferences. **Check recognised URL extensions** rejects a conflicting ebook suffix on the final URL. A URL with no recognised suffix can pass when its response has a matching ebook signature. **Require a compatible Content-Type** also requires an appropriate or generic binary response header. **Open store in external web browser** uses your normal browser to open store pages. Tags apply to downloads handled by Calibre’s embedded store window.

## Searching and downloading

In **Get books**, search by title, author, or ISBN. Results show a download icon when the plugin can verify a public file. It follows ordinary HTTP redirects and checks the start of the response for the reported format. Calibre gets one verified link per format. Before importing, the plugin checks the complete file against the Content-Length when supplied and the Archive record's MD5 when the record has one.

The plugin can resolve public links exposed directly on a record page and supported LibGen, Sci-Hub, and Z-Library landing pages. When a provider explicitly reports that a link expired, the plugin reopens the Archive record once to get a fresh link for the selected format. Partner-server links, browser verification, sign-in, borrowing, and torrent listings require the website. When a record has no verified download, opening the result takes you to its downloads section in your normal browser.

Covers and downloads use the same guarded HTTP client. Direct requests connect to an IP address checked after DNS resolution, and the same rules apply at each redirect. If you use a system proxy, the plugin checks local DNS answers for extracted hosts and the proxy controls its own upstream connections. Background requests are limited to two per host. Concurrent lookups for the same record share work while it is in progress, and rate-limited hosts pause according to the server's `Retry-After` header.

## Troubleshooting mirror failures

When all mirrors fail, **Get books** displays the cause and offers **Open in browser**. You can also open the **Mirrors** tab and test a selected mirror. A valid search with zero results passes the test. A timeout, HTTP error, browser verification redirect, or parked domain fails it. Check an address against the [site’s published mirror list](https://annas-archive.gl/faq#mirrors) before adding it. Mirror availability and website access restrictions vary by network and region.

For details, start Calibre with `calibre-debug -g`, run a search, and inspect the log for the mirror host and failure category. The plugin logs without search terms, credentials, or full download URLs.

Live search and download success remain unverified. A filtered search for *Pride and Prejudice* hit browser verification or HTTP 403 on all bundled mirrors from the test environment. The automated tests use synthetic search and detail pages.

## Building and testing

The build uses Python 3.8 or later and the standard library.

```sh
python build_plugin.py
```

The script builds `dist/calibre_annas_archive-v<VERSION>.zip` with the Calibre import marker and Python modules at the ZIP root. Use `--output-dir` to change the destination. The ZIP is reproducible from the same source files and Python toolchain.

```sh
python -m pip install lxml
python -m compileall -q .
python -m unittest discover -v
```

CI runs syntax checks and Python unit tests on Linux, Windows, and macOS. It also checks ZIP reproducibility and uploads the build. The Windows and macOS smoke jobs install Calibre, load the ZIP, and exercise the store with offline responses. To reproduce that check, use an empty Calibre configuration directory and run these commands from the checkout. This example uses a POSIX shell.

```sh
export CALIBRE_CONFIG_DIRECTORY="$(mktemp -d)"
calibre-customize -a dist/calibre_annas_archive-v0.3.0.zip
calibre-debug -e tests/calibre_smoke.py
```

On headless Linux, set `QT_QPA_PLATFORM=offscreen` before running the smoke test. The script exercises preferences, background mirror tests, the search warning, browser fallback, cover loading, an expired-link refresh, and EPUB import into a temporary Calibre library.

To check the live site from Calibre, open **Customize plugin**, test a mirror, move it to the top, and save. Search for a public-domain title such as *Pride and Prejudice* with the English and EPUB filters. Select an edition you may download and confirm its EPUB appears in your library. Record the mirror, Calibre version, and result. For regression fixtures, keep the relevant search and detail markup and remove tokens, cookies, account data, and signed download URLs before committing it.

## Attribution and licensing

ScottBot10 wrote the original [Anna’s Archive Calibre Store](https://github.com/ScottBot10/calibre_annas_archive). The latest upstream tag and pre-release checked for this work were `v0.2.4`. The upstream repository has no published licence file. Confirm redistribution rights before making a public release.
