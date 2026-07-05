# FeatureGen frontend

The catalog UI for FeatureGen: Upload sources, Search the catalog, work the Review queue
(quarantine), and derive features in the Workbench. React + TypeScript + Vite, talking to the
FastAPI backend.

## Develop

From the repo root, run `make api` in one terminal and `make frontend-dev` in another. The dev
server proxies API calls to the FastAPI server on `:8000`.

## Test / build

- `npm test` — Vitest component suite.
- `npm run build` — type-check and produce the production bundle.

See the root `README.md` **Frontend** section for the end-to-end walkthrough.
