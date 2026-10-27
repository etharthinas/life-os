# life-os desktop

An Electron shell around the server (https://lifeos.tail969156.ts.net, Tailscale must be on), so it's
the same app as the iPhone PWA. Adds: ⌘⇧Space quick capture from anywhere (⏎ journal, ⌘⏎ todo),
a menu-bar icon, remembered window size, links opening in the browser.

```sh
cd desktop && npm install
npm start                      # run from source
npm run dist                   # build dist/mac-arm64/life-os.app
codesign --force --deep -s - dist/mac-arm64/life-os.app   # ad-hoc sign (Apple Silicon needs one)
cp -R dist/mac-arm64/life-os.app /Applications/
```

`LIFEOS_URL=http://localhost:8000 npm start` points it at a local dev server.
