/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FX_SRC_POLICY_H
#define FX_SRC_POLICY_H
#include "worker.h"
int fx_src_load_profile(enum fx_profile *);
const char *fx_src_profile_name(enum fx_profile);
#endif
