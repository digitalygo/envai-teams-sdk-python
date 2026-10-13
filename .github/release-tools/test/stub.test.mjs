import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as stub from '../stubs/semantic-release-npm/index.js';

const hooks = ['verifyConditions', 'prepare', 'publish', 'addChannel'];

for (const hook of hooks) {
  test(`${hook} throws discrete-npm-publication error`, () => {
    assert.throws(() => {
      stub[hook]();
    }, /npm publication is disabled/);
  });
}
