# CertBound BA Customer Kickoff Scene Assets

Scenario: CB-SC-001 — Customer Kickoff  
Version: 0.3.0-twelve-scene-kickoff

## Contents

- `scenes/`: 12 app-ready PNG scene images at 1600×800.
- `references/canonical-simulator-ui-reference.png`: approved Simulator UI source of truth.
- `references/business-analyst-character-reference-guide.png`: approved character continuity guide.
- `references/customer-kickoff-12-scene-visual-guide.png`: approved 12-scene storyboard.
- `manifest.json`: stable mapping from scene IDs to image filenames.

## Repository destination

Copy this folder to:

`assets/scenarios/business_analyst/cb-sc-001/`

Preserve the filenames in `manifest.json`. The application should resolve images by scene ID and use a safe fallback when an asset is missing.
