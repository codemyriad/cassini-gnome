// SPDX-License-Identifier: GPL-3.0-or-later
// Private Chromium fixture entrypoint: run the unchanged production worker and
// expose its supported constructors to trusted adapter tests (MV3 forbids
// dynamic import in workers). The shipped extension never installs this file.
import './background.js';
import {AppControl} from './app-control.js';
import {CallSignalProducer, SiteSignalAdapters} from './call-signals.js';
globalThis.fixtureApi = {AppControl, CallSignalProducer, SiteSignalAdapters};
