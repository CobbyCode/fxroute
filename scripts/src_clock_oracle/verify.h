/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FX_CLOCK_ORACLE_H
#define FX_CLOCK_ORACLE_H
#include "resample.h"
void fx_verify_init(struct resample *);
void fx_verify_match(double,const void *SPA_RESTRICT [],unsigned,unsigned,unsigned,unsigned,void *SPA_RESTRICT []);
void fx_verify_account(unsigned,unsigned,double,int,int);
void fx_verify_complete(void);
#endif
