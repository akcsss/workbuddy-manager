import assert from 'node:assert/strict';
import {availabilityOf, availabilityLabelKey, availabilityTitleKey} from './account-status.ts';

const account = {uid:'paused', manual_disabled:true, manual_reason:'account-risk: token_refresh HTTP 429'};
assert.equal(availabilityOf(account), 'manualDisabled');
assert.equal(availabilityLabelKey('manualDisabled', account), 'risk.paused');
assert.equal(availabilityTitleKey('manualDisabled', account), 'risk.explanation');
const manual = {...account, manual_reason:'operator pause'};
assert.equal(availabilityLabelKey('manualDisabled', manual), 'accounts.badgeManualDisabled');
assert.equal(availabilityTitleKey('manualDisabled', manual), 'accounts.badgeManualDisabledTitle');
console.log('PASS: safety pause badge and explanation differ from ordinary manual pause');
