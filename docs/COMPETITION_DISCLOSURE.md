# Competition disclosure

## Important eligibility fact

This repository’s implementation was built overnight before the event and outside the
on-site build window with substantial Codex assistance. The supplied event context states
that implementation work must happen on-site and that remote code contributions are not
permitted.

Therefore:

- do not describe this codebase as created entirely on-site;
- do not hide or rewrite commit timestamps/provenance;
- do not submit it as a fresh on-site build unless the organizers explicitly confirm that
  this starting point is eligible;
- do not characterize the overnight implementation as research, a starter scaffold, or
  cosmetic preparation when it contains the product’s working core;
- do not imply a live Sarvam deployment or call exists until it has actually been tested.

The safe path is to disclose the facts before judging and ask the organizer for an explicit
eligibility decision. If it is ineligible, use it as a learning prototype or non-competing
demo, or rebuild only under rules the organizer confirms in writing.

## Suggested organizer note

> Agent Hotline’s concept and repository implementation were developed before the on-site
> build period, including an overnight Codex-assisted implementation. The project uses
> Sarvam Samvaad as its intended primary voice runtime. We are disclosing this before
> submission because the event rules provided to us say implementation must occur on-site.
> Please confirm whether the repository may be used as a starting point, must be excluded,
> or may be demonstrated only outside competition.

Do not soften this wording by claiming that only “planning” occurred if source code, tests,
plugin packaging, or deployment configuration were created overnight.

## Technical status to disclose

At the documentation snapshot:

- Windows/Python local development and deterministic tests exist;
- Codex `0.144.6` direct stdio App Server support exists;
- Claude Code `2.1.211` can use the shared MCP contract;
- Sarvam credentials, organization/workspace, active Vobiz connection, provisioned number,
  draft app version `1`, and HTTP-tool entitlement were verified;
- no inbound deployment had been created;
- no completed end-to-end Hotline call had yet been evidenced;
- a Cloudflare quick tunnel was available but its public URL was not committed;
- all committed operational runbooks were mock-only.

Update this list with dated, truthful results before any presentation. Separate
“implemented locally,” “account capability verified,” and “demonstrated live.”

## Provenance record

Preserve:

- the full Git history and timestamps;
- the original planning/context file;
- tool/agent-generated code provenance where available;
- test and deployment logs with secrets redacted;
- the time and result of organizer eligibility guidance;
- the time of each genuine Sarvam call/deployment test.

Never fabricate earlier or later dates, squash history to conceal origin, or present a
recording as a live call.

## Privacy in disclosure

The disclosure should not contain phone numbers, API keys, confirmation secrets, provider
identifiers, callback URLs, or raw transcripts. Refer to them by environment-variable name
or as verified opaque account values.

## Submission checklist

- [ ] Organizer has been told that implementation occurred overnight/off-site.
- [ ] Eligibility answer is recorded.
- [ ] Repository history remains intact.
- [ ] AI coding assistance is disclosed if the submission form asks.
- [ ] Sarvam is accurately described as central to the demonstrated voice loop.
- [ ] Native call/deployment/tool claims have evidence.
- [ ] Simulated, recorded, and live portions are labeled separately.
- [ ] No personal data or secrets appear in slides, video, logs, or repository.
