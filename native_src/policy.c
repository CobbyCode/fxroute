/* SPDX-License-Identifier: AGPL-3.0-only */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <ctype.h>
#include <spa/utils/json.h>
#include "policy.h"

const char *fx_src_profile_name(enum fx_profile profile)
{
    switch(profile) {
    case FX_LOW: return "low";
    case FX_STANDARD: return "standard";
    case FX_HIGH: return "high";
    default: return "invalid";
    }
}
int fx_src_load_profile(enum fx_profile *profile)
{
    char path[PATH_MAX], text[1024], key[64], value[64];
    const char *root=getenv("XDG_CONFIG_HOME"), *home=getenv("HOME");
    int length;
    if(root&&*root)length=snprintf(path,sizeof(path),"%s/fxroute/src-policy.json",root);
    else if(home&&*home)length=snprintf(path,sizeof(path),"%s/.config/fxroute/src-policy.json",home);
    else return -ENOENT;
    if(length<0||(size_t)length>=sizeof(path))return -ENAMETOOLONG;
    FILE *file=fopen(path,"r");
    if(!file){if(errno!=ENOENT)return -errno;*profile=FX_STANDARD;return 0;}
    size_t size=fread(text,1,sizeof(text),file);
    int error=ferror(file);fclose(file);
    if(error||size==sizeof(text))return -EINVAL;
    const char *cursor=text,*end=text+size,*token;
    while(cursor<end&&isspace((unsigned char)*cursor))++cursor;
    if(cursor==end||*cursor++!='{')return -EINVAL;
    while(cursor<end&&isspace((unsigned char)*cursor))++cursor;
    struct spa_json iterator;spa_json_init(&iterator,cursor,end-cursor);
    int len=spa_json_next(&iterator,&token);
    if(len<2||token!=cursor||*token!='"'||token[len-1]!='"'||spa_json_parse_stringn(token,len,key,sizeof(key))<0||strcmp(key,"mode"))return -EINVAL;
    cursor=token+len;
    while(cursor<end&&isspace((unsigned char)*cursor))++cursor;
    if(cursor==end||*cursor++!=':')return -EINVAL;
    while(cursor<end&&isspace((unsigned char)*cursor))++cursor;
    spa_json_init(&iterator,cursor,end-cursor);len=spa_json_next(&iterator,&token);
    if(len<2||token!=cursor||*token!='"'||token[len-1]!='"'||spa_json_parse_stringn(token,len,value,sizeof(value))<0)return -EINVAL;
    cursor=token+len;
    while(cursor<end&&isspace((unsigned char)*cursor))++cursor;
    if(cursor==end||*cursor++!='}')return -EINVAL;
    while(cursor<end&&isspace((unsigned char)*cursor))++cursor;
    if(cursor!=end)return -EINVAL;
    if(!strcmp(value,"low"))*profile=FX_LOW;
    else if(!strcmp(value,"standard"))*profile=FX_STANDARD;
    else if(!strcmp(value,"high"))*profile=FX_HIGH;
    else return -EINVAL;
    return 0;
}
