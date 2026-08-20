# ADR 0001: Disc-aware TV ingest and organization

- Status: Accepted
- Date: 2026-08-19
- Decision owners: Project maintainers

## Context

The current TV post-processing service watches a rip directory and asks
FileBot to identify and move files. This makes the filename the main source of
identity and episode order. Optical-disc filenames and ripper-generated title
numbers are not reliable enough for that purpose.

DVD and Blu-ray discs contain stronger structural evidence. Depending on the
disc, this can include title tables, playlists, clip boundaries, navigation
commands, chapters, menu behavior, language tracks, and optional descriptive
metadata. The existing `tv/organizer/bdmv.py` and
`tv/organizer/mpls.py` parsers already expose part of this information for
Blu-ray discs.

The system must:

- resolve whether an ingest is a TV show before it can be organized;
- resolve a stable show identity, not just a display name;
- suggest a season and episode order when the evidence supports them;
- expose all unresolved and suggested values for human review;
- produce Jellyfin-compatible paths only after the required assignments have
  been resolved;
- explain every automated suggestion;
- tolerate restarts, repeated events, and the same disc being inserted again;
- preserve distinct editorial cuts while detecting redundant title tracks;
- avoid deleting, overwriting, or publishing uncertain media automatically.

Disc authoring varies significantly. HDMV navigation, BD-J applications, and
the DVD navigation virtual machine require different analysis techniques.
Some discs contain no useful show or season text. A disc can also contain:

- extras whose duration resembles an episode;
- multiple playlists that resolve to the same video;
- seamless-branching playlists that share most clips;
- regular, extended, censored, broadcast, or director's cuts of one episode;
- multi-angle versions;
- one title containing multiple episodes;
- one episode split across multiple titles;
- deliberately duplicated or obfuscated playlists.

Consequently, the workflow can require a resolved answer without claiming
that every answer can be inferred automatically.

## Decision

Replace automatic filename-based post-processing with a durable,
evidence-driven ingest, review, and commit workflow.

ARM remains the sole owner of optical-drive detection, mounting, ripping, and
ejection. A collector integrated with the ARM job lifecycle captures disc
metadata before ejection. A separate, unprivileged application imports the
capture and completed rip, runs analyzers, presents suggestions in a review
UI, and organizes only approved assignments.

The high-level state machine is:

```text
disc_seen
  -> capturing
  -> ripping
  -> awaiting_assets
  -> analyzing
  -> needs_review
  -> approved
  -> organizing
  -> complete
```

Any operational state can transition to `failed`. Failed and interrupted jobs
are resumable. An explicit ARM completion signal or marker advances a job from
`ripping`; file age or a fixed settle delay is not considered proof that a rip
is complete.

### 1. Process boundaries

The system is divided into three trust and responsibility boundaries:

1. **Disc collector**
   - Runs with only the device and mount privileges required by ARM.
   - Captures navigation metadata and ripper title information.
   - Does not organize the Jellyfin library.
2. **Analyzer and review service**
   - Reads completed rip assets and captured manifests.
   - Stores jobs, evidence, suggestions, and user decisions.
   - Has no optical-device access.
3. **Organizer worker**
   - Receives only approved, validated organization plans.
   - Has narrowly scoped write access to staging and library roots.
   - Cannot write outside configured roots.

For a single server and drive, the initial implementation will use a Python
service, SQLite in WAL mode, and a durable single-worker queue. The HTTP API
and server-rendered UI may run in the same application, but background job
state must remain in the database rather than process memory.

### 2. Capture and identity

The collector writes an immutable, versioned disc manifest into the job's
staging directory. It records hashes of every captured input and the versions
of the collector, parsers, ARM, and ripper.

For Blu-ray, capture at least:

- `index.bdmv` and `MovieObject.bdmv`;
- every MPLS and CLPI file;
- `META/DL` metadata when present;
- relevant BDJO/application metadata when present;
- the optical volume label and filesystem metadata;
- MakeMKV robot-mode disc/title/stream output;
- ARM's source job identifier and identification evidence.

For DVD, capture at least:

- the VIDEO_TS IFO and BUP files;
- title-set, PGC, cell, chapter, angle, and VM-command information;
- the optical volume label and filesystem metadata;
- MakeMKV robot-mode disc/title/stream output;
- enough menu/navigation evidence to support a later DVD navigation trace.

Large menu or application assets should be copied only when a configured
analyzer needs them. Runtime navigation traces should normally execute while
the readable disc source is still available.

A disc fingerprint is based on stable structural inputs such as navigation
file hashes, title/playlist topology, and disc table-of-contents data. The
volume label alone is not an identity. The fingerprint makes repeated insert
events and reprocessing idempotent without preventing an intentional re-rip.

Each ripped asset must retain a deterministic association with its source
title, playlist, PGC, or angle. Ripper title identifiers and reported source
filenames are primary evidence. Duration, chapters, streams, and content
fingerprints are independent validation evidence, not the sole mapping method.

### 3. Canonical domain model

The core model distinguishes physical structure, media content, editorial
versions, and library assignments:

- **Disc**: one fingerprinted optical-disc structure.
- **Source title**: a navigable Blu-ray title/playlist or DVD title/PGC.
- **Rip asset**: an output media file and its source-title association.
- **Content candidate**: an inferred episode, multi-episode title, extra, menu,
  or unknown item.
- **Content equivalence group**: source titles or assets believed to present
  the same underlying episode content.
- **Edition**: a materially distinct editorial cut within an equivalence
  group, such as `Regular`, `Director's Cut`, or `Extended`.
- **Episode assignment**: a mapping between one or more assets and one or more
  canonical episode numbers.
- **Suggestion**: an analyzer proposal with evidence and confidence.
- **Decision**: a user acceptance, rejection, correction, or explicit choice.
- **Library snapshot**: the existing show/season/episode occupancy observed
  when analysis or commit validation ran.

Episode assignments are many-to-many. This supports multi-episode files,
split episodes, multiple editions, and replacements without distorting the
source-title model.

Show identity is stored as a provider-qualified stable identifier, initially
TMDB TV ID, plus its display name and first-air year. A normalized name is not
a primary key.

### 4. Analyzer contract and decision log

Analyzers consume the immutable manifest, rip-asset metadata, selected catalog
data, and a point-in-time library snapshot. They do not rename or move files.

Every analyzer emits zero or more structured suggestions:

```json
{
  "kind": "episode_order",
  "value": ["asset-a", "asset-b"],
  "confidence": 0.96,
  "evidence": [],
  "contradictions": [],
  "analyzer": "hdmv_navigation",
  "analyzer_version": "1.0.0",
  "input_manifest_hash": "..."
}
```

Evidence and contradictions contain machine-readable rule identifiers,
weights or likelihood contributions, and human-readable explanations. A
suggestion is reproducible from its recorded inputs. Reanalysis creates a new
suggestion revision rather than rewriting prior reasoning or user decisions.

Confidence is field-specific. The system does not combine TV classification,
show identity, season, edition, and episode order into one opaque score.
Thresholds are policy settings and must be calibrated against a regression
corpus of real discs.

### 5. TV, show, and season resolution

TV classification can use repeated episode-like runtimes, common stream
layouts, navigation chains, title counts, menu evidence, and catalog
candidates. No single runtime threshold is sufficient because films can have
many extras and TV episodes can be unusually short or long.

Show-name candidates can come from `META/DL`, volume labels, ARM and MakeMKV
disc titles, menu or credit OCR, and catalog searches. Labels and directory
names are weak evidence unless corroborated. A high-confidence show suggestion
requires an unambiguous provider match supported by more than one independent
signal.

Season detection can use explicit disc text, menu OCR, catalog episode counts
and runtimes, disc numbering, and existing-library context. It normally
requires human confirmation unless the disc explicitly identifies the season
and the catalog match is unambiguous.

TV classification and show identity are required before approval. Season and
episode assignment are required before Jellyfin organization. A job with an
unknown value remains in `needs_review`; the requirement is a workflow
invariant, not a promise of automatic inference.

### 6. Play-all and episode-order analysis

Play-all behavior is isolated behind this interface:

```text
infer_order(manifest, source_titles, trace?) -> OrderSuggestion[]
```

Initial strategy modules are:

- `hdmv_navigation`: semantic HDMV command decoding and symbolic control-flow
  analysis;
- `bdj_runtime_trace`: sandboxed BD-J/menu execution with playlist-event
  recording;
- `dvd_navigation`: DVD VM analysis or a libdvdnav-backed interaction trace;
- `playlist_heuristics`: duration, chapter, stream, title-table, and clip-order
  inference, capped below high confidence when used alone.

An episode-order suggestion may be high confidence only when:

- a navigation path or trace yields one unambiguous sequence;
- every selected episode candidate occurs exactly once;
- no unresolved branch, cycle, angle, or register state can change the order;
- every source title maps unambiguously to a rip asset;
- independent duration, chapter, clip, and stream evidence agrees; and
- no edition or duplicate relationship has been flattened to create the
  sequence.

Disc playback order and canonical catalog numbering are stored separately.
TMDB DVD episode groups may inform a mapping, but the configured Jellyfin
metadata provider's canonical season/episode numbering controls the final
`SxxEyy` assignment unless an explicit alternate-order policy is selected.

### 7. Duplicate titles and editorial editions

The system must not treat every similar title as a duplicate. Deduplication is
an inference and review problem with three possible outcomes:

1. **Exact duplicate**: byte-identical rip assets, or source titles with
   identical effective clip/cell ranges, angle selection, and stream content,
   confirmed by aligned content fingerprints.
2. **Probable duplicate**: highly similar assets without enough evidence to
   exclude a small editorial or stream difference.
3. **Distinct edition**: assets map to the same canonical episode but contain a
   material editorial difference, including added, removed, replaced, or
   reordered video segments.

Analyzer inputs include:

- cryptographic file hashes for exact output duplicates;
- normalized ordered clip/cell identifiers and in/out ranges;
- duration and chapter-boundary fingerprints;
- selected angle and seamless-branch path;
- video, audio, subtitle, language, and commentary stream sets;
- aligned video/audio fingerprints or sampled frame hashes;
- shared-segment ratios and the location of differing segments;
- explicit menu, playlist, ripper, or OCR labels such as `Director's Cut`.

The following policies apply:

- A different playlist number or ripper title number is never sufficient to
  declare a duplicate.
- Similar duration is weak evidence and is never sufficient to declare a
  duplicate.
- Identical video with a materially different audio or subtitle selection is
  reported as a stream variant; it is not automatically deleted or promoted
  to an editorial edition.
- Multi-angle presentations remain angle variants unless content analysis or
  explicit labels support an edition classification.
- Playlists sharing most clips but selecting different branch segments are
  edition candidates, even when their total durations are close.
- A regular cut and a director's cut receive separate `Edition` records and
  may both map to the same episode assignment.
- No probable duplicate or edition is automatically deleted, replaced, or
  excluded from the library.
- Automatic suppression is allowed only for an exact duplicate under a
  separately enabled policy, and still retains the source/evidence record and
  a recoverable staged asset until commit retention rules run.

The review UI groups related titles and presents synchronized differences:
duration delta, chapters, streams, shared segments, unique segments, labels,
and analyzer reasoning. The user can choose:

- keep one asset and mark the others as duplicates;
- keep all assets as named editions;
- select a preferred/default edition;
- merge compatible stream variants in a later remux workflow;
- leave the relationship unresolved.

Unresolved probable duplicates block automatic episode-order confidence when
including or excluding them would change the proposed sequence.

The core model always preserves editions independently of the target media
server. Jellyfin export uses a versioned capability profile:

- when the installed Jellyfin version supports multiple versions of TV
  episodes, export all approved editions using its supported episode-version
  naming convention;
- otherwise, require an explicit policy to publish only the preferred edition
  and retain other editions in managed staging or a separately configured
  library location;
- never mislabel a director's cut as an extra merely to make Jellyfin ingest
  it;
- never silently discard an approved edition.

### 8. Existing-library context

The library index records provider show ID, season number, episode coverage,
parts, editions, file path, and file identity. The analyzer reports gaps,
occupied episode numbers, and conflicts.

Existing episodes do not prove that the new disc follows them.
`max(existing) + 1` is exposed only as a suggestion and is confidence-capped
unless disc numbering, navigation order, and catalog data independently
support it. Users
can set an anchor or starting episode, after which the approved relative order
can populate subsequent assignments.

Analysis never changes the library. The organizer refreshes the library
snapshot immediately before commit and rejects a stale plan if its targets are
now occupied or changed.

### 9. Review and approval invariants

A job can enter `approved` only when:

- media type is resolved as TV;
- a provider-backed show identity is selected;
- a season is selected;
- every publishable asset has an episode, multi-episode, part, edition, extra,
  duplicate, or ignore disposition;
- every episode number collision is explicitly resolved;
- every probable duplicate that affects organization is resolved;
- a preferred edition is selected when the target export profile cannot
  publish multiple editions; and
- the complete target plan passes path and naming validation.

Users may accept individual suggestions, manually enter values, drag episode
order, assign a starting episode, compare editions, and reject suggestions.
Manual decisions are recorded separately from analyzer output.

### 10. Jellyfin export and filesystem commit

The default layout is:

```text
Show Name (Year) [tmdbid-ID]/
  Season 01/
    Show Name (Year) S01E01 Episode Title.mkv
```

The exporter also supports Jellyfin multi-episode and multipart conventions.
Edition filenames are produced only by a Jellyfin capability profile verified
against the installed server version.

Before modifying the filesystem, the organizer creates a dry-run plan listing
every source, target, edition label, directory creation, and conflict. Target
paths are derived from validated components and constrained to configured
roots; user-provided names cannot introduce arbitrary paths.

Commit rules are:

- never overwrite an existing file;
- prefer an atomic rename of a complete prepared directory on one filesystem;
- across filesystems, copy to a temporary target, verify size and hash, flush,
  rename into place, and remove the source only after successful verification;
- record every operation and its outcome;
- retain enough information for a compensating rollback where feasible;
- notify Jellyfin or request a library scan only after the commit succeeds.

Unapproved and partially committed media remains outside the watched Jellyfin
library.

### 11. TMDB integration

The backend performs TMDB TV search and retrieves series, season, episode, and
episode-group data. API credentials remain server-side. Responses are cached
with request parameters, locale, retrieval time, and provider IDs. The client
handles transient errors and `429` responses without blocking local review of
already cached jobs.

The UI includes the attribution required by TMDB. Catalog changes do not
rewrite previously approved decisions automatically.

### 12. Delivery sequence

Implementation proceeds in this order:

1. Define the manifest and domain schemas, then build a minimal collector.
2. Build a regression corpus covering HDMV, BD-J, DVD, extras, obfuscation,
   multi-episode titles, duplicate playlists, multi-angle titles, and multiple
   editorial cuts.
3. Implement asset/source mapping and exact/probable duplicate analysis.
4. Implement static HDMV navigation and structured decision logs.
5. Add persistent jobs, TMDB lookup, library indexing, and the review UI.
6. Add dry-run planning and transactional Jellyfin organization.
7. Add DVD navigation and BD-J runtime tracing.
8. Calibrate confidence thresholds from confirmed corpus decisions.

## Consequences

### Positive

- Disc structure, rather than unreliable ripped filenames, becomes the main
  source of playback-order evidence.
- Uncertainty is visible and reviewable instead of being converted into a
  confident but incorrect filename.
- The same captured disc can be reanalyzed after analyzer improvements without
  reinserting or reripping it.
- Hardware privileges are isolated from the web application and library
  organizer.
- Stable provider IDs improve catalog and Jellyfin matching.
- Regular and director's cuts can coexist without being mistaken for redundant
  playlists.
- Filesystem commits are auditable, collision-safe, and restart-tolerant.

### Negative

- The system is larger than a filesystem watcher and requires persistent job
  state, a review queue, and database migrations.
- Some discs cannot be identified without human input or OCR/runtime tracing.
- BD-J and DVD menu tracing add dependencies and require strict timeouts and
  sandboxing.
- Duplicate and edition comparison may require sampling or decoding media,
  increasing analysis time.
- Supporting multiple episode editions depends partly on the installed
  Jellyfin version and client behavior.

### Risks

- Confidence scores can appear more precise than the underlying evidence.
  Mitigation: expose evidence, calibrate against a corpus, and retain hard
  eligibility rules for high-confidence suggestions.
- Ripper title-to-file mappings can drift across ripper versions. Mitigation:
  preserve raw robot output and independently validate mappings.
- Existing library state can change during review. Mitigation: snapshot for
  analysis and revalidate immediately before commit.
- A duplicate detector can discard a rare edition. Mitigation: default to
  preservation, require review, and permit automatic suppression only for
  exact duplicates under an explicit policy.

## Alternatives considered

### Continue using FileBot as the primary detector

Rejected because filename and ripper title order remain the primary evidence,
which does not solve the observed ordering problem.

### Fully automatic organization from playlist duration and number

Rejected because extras, obfuscated playlists, multiple cuts, and multi-angle
titles make these heuristics unsafe.

### Independently watch and mount the drive from the organizer

Rejected because ARM already owns disc detection and ripping. Two independent
device consumers introduce mount, read, and eject races.

### Keep only the longest or first copy of similar titles

Rejected because a longer title may be an extended or director's cut and a
lower-numbered playlist has no universal semantic priority.

### Require users to identify and order every file manually

Rejected because navigation and structural metadata can provide valuable,
explainable suggestions even though human review remains necessary.

## References

- [Automatic Ripping Machine](https://github.com/automatic-ripping-machine/automatic-ripping-machine)
- [Jellyfin TV show organization](https://jellyfin.org/docs/general/server/media/shows/)
- [Jellyfin metadata provider identifiers](https://jellyfin.org/docs/general/server/metadata/identifiers/)
- [TMDB TV search](https://developer.themoviedb.org/reference/search-tv)
- [TMDB TV season details](https://developer.themoviedb.org/reference/tv-season-details)
- [TMDB episode groups](https://developer.themoviedb.org/reference/tv-episode-group-details)
- [VideoLAN libbluray](https://images.videolan.org/developers/libbluray.html)
- [VideoLAN libdvdnav](https://images.videolan.org/developers/libdvdnav.html)
