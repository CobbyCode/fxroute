# FX-LIBRARY-PLAYLIST-SEARCH-001 — Add name-only playlist search to Library search

## Status
review

## Goal
Make FXRoute's Library search find playlists by partial playlist name without adding noisy playlist-content matches to the normal track results.

## Background
Paul wants playlist discovery through the existing Library search field, but only when the playlist name itself matches. Songs should not appear merely because they are members of matching playlists; a song can be in many playlists, and that would make search results noisy.

Planning note:
- `outputs/fxroute-playlist-name-search-plan-2026-05-15.md`

## Scope
Implement v1 as a frontend-only change in `static/app.js` unless inspection shows a backend gap.

Expected behavior:
- Empty search: existing playlist and track display remains unchanged.
- Active search in Library → Tracks view:
  - existing search behavior remains otherwise unchanged;
  - show playlists whose names contain the query, case-insensitive;
  - example: searching `mix` should show a playlist whose name contains `mix`;
  - show normal track matches using existing track search fields;
  - do not match playlist contents;
  - do not duplicate songs by playlist membership.
- Folder view behavior should remain unchanged; playlists are not displayed there today and should not be introduced there.
- Purpose: make many saved playlists quickly accessible by name.

## Implementation notes
Likely changes:
- Add `playlistMatchesLibraryQuery(playlist, query)`.
- Add/use filtered playlist list in `renderTracks()`.
- Change playlist rendering condition from "only when no search" to "when non-folder mode and filtered playlists exist".
- Include filtered playlists in the search empty-state decision.
- Update search empty-state text to mention tracks/playlists.
- Optionally update Library info text with playlist match count.

## Acceptance criteria
- Searching part of a playlist name shows that playlist.
- A query matching only a playlist name still shows a playable playlist row even if zero tracks match.
- Track results are not expanded by playlist membership.
- Existing playlist row actions still work after filtering: play/load, export, delete.
- Existing track search remains intact.
- Existing folder view remains intact.
- Run at least:
  - `node --check static/app.js`
  - `git diff --check`

## Output expected
- Code changes in the FXRoute public repo.
- Brief implementation note or commit-ready summary.

## Implementation summary
- Added frontend-only playlist name filtering for Library search in Tracks mode.
- Playlist rows now remain hidden in Folder mode, and active searches only match playlist names (not playlist contents).
- Search empty-state and Library info text now account for matching playlists.

## Validation
- `node --check static/app.js` — passed
- `git diff --check` — passed
