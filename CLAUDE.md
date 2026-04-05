# gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Install gstack (one-time, per machine):
```bash
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack && cd ~/.claude/skills/gstack && ./setup
```

Available gstack skills:
- `/office-hours` — structured async advice session
- `/plan-ceo-review` — CEO-level plan review
- `/plan-eng-review` — engineering plan review
- `/plan-design-review` — design plan review
- `/design-consultation` — design consultation
- `/design-shotgun` — rapid design exploration
- `/design-html` — HTML/CSS design generation
- `/review` — code review
- `/ship` — ship a feature end-to-end
- `/land-and-deploy` — land and deploy changes
- `/canary` — canary deploy
- `/benchmark` — performance benchmarking
- `/browse` — web browsing (use this for all web browsing)
- `/connect-chrome` — connect to a Chrome browser instance
- `/qa` — QA testing
- `/qa-only` — QA without shipping
- `/design-review` — review a design
- `/setup-browser-cookies` — set up browser cookies
- `/setup-deploy` — set up deployment
- `/retro` — retrospective
- `/investigate` — investigate an issue
- `/document-release` — document a release
- `/codex` — run Codex
- `/cso` — chief security officer review
- `/autoplan` — automatically generate a plan
- `/careful` — careful/cautious mode
- `/freeze` — freeze the codebase
- `/guard` — guard mode
- `/unfreeze` — unfreeze the codebase
- `/gstack-upgrade` — upgrade gstack to latest
- `/learn` — learn about a topic

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
