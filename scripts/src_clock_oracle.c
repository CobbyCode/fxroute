/* SPDX-License-Identifier: AGPL-3.0-only */
/* Test-only approved-engine PCM plus independent native matcher oracle. */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "src_clock_oracle/verify.h"

static struct resample oracle;
static float *nominal,*output[18],*input[18];
static unsigned frames,channels,output_rate,match_calls;
static uint64_t cursor,accepted,lead_frames;
static double nominal_lead,last_padding_rate;
static FILE *target;

void fx_verify_init(struct resample *r)
{
    assert(r->channels==18);channels=r->channels;output_rate=r->o_rate;
    oracle=(struct resample){.i_rate=r->o_rate,.o_rate=r->o_rate,.channels=channels,
        .quality=r->quality,.options=r->options,.config=r->config,.cpu_flags=r->cpu_flags};
    assert(!resample_native_init(&oracle));
    FILE *file=fopen(getenv("FXROUTE_VERIFY_NOMINAL"),"rb");assert(file);
    assert(!fseek(file,0,SEEK_END));long bytes=ftell(file);rewind(file);
    assert(bytes>0&&bytes%(channels*sizeof(float))==0);frames=bytes/(channels*sizeof(float));
    nominal=calloc(bytes,1);assert(nominal&&fread(nominal,1,bytes,file)==bytes);fclose(file);
    for(unsigned c=0;c<channels;++c){input[c]=calloc(16384,4);output[c]=calloc(16384,4);assert(input[c]&&output[c]);}
    target=fopen(getenv("FXROUTE_VERIFY_OUTPUT"),"wb");assert(target);
}

void fx_verify_match(double rate,const void *SPA_RESTRICT src[],unsigned offered,unsigned capacity,unsigned used,unsigned made,void *SPA_RESTRICT dst[])
{
    assert(offered<=16384&&capacity<=16384);
    for(unsigned c=0;c<channels;++c)for(unsigned i=0;i<offered;++i) {
        float expected=cursor+i<frames?nominal[(cursor+i)*channels+c]:0;
        assert(((const float *)src[c])[i]==expected&&"Nominal PCM differs from the approved-engine reference");
        input[c][i]=expected;
    }
    unsigned take=offered,produce=capacity;resample_update_rate(&oracle,rate);
    resample_process(&oracle,(const void **)input,&take,(void **)output,&produce);
    assert(take==used&&produce==made);
    for(unsigned c=0;c<channels;++c)assert(!memcmp(dst[c],output[c],made*4));
    for(unsigned i=0;i<made;++i)for(unsigned c=0;c<channels;++c)assert(fwrite(&output[c][i],4,1,target)==1);
    cursor+=take;++match_calls;
}

void fx_verify_account(unsigned used,unsigned made,double rate,int eof,int prime)
{
    accepted+=used;
    if(!match_calls&&made){lead_frames+=made;nominal_lead+=made/rate;last_padding_rate=rate;}
}

void fx_verify_complete(void)
{
    assert(accepted==96000&&cursor==frames+resample_delay(&oracle));
    assert(nominal_lead>=36000-1e-6&&nominal_lead<36000+1/last_padding_rate+1e-6);
    unsigned take=0,made=16384;resample_process(&oracle,(const void **)input,&take,(void **)output,&made);
    assert(!take&&!made&&match_calls);
    assert(!fclose(target));
    printf("CLOCK_ORACLE {\"lead_frames\":%lu,\"nominal_lead\":%.9f,\"consumed\":%lu}\n",lead_frames,nominal_lead,cursor);
    resample_free(&oracle);free(nominal);
    for(unsigned c=0;c<channels;++c){free(input[c]);free(output[c]);}
}
