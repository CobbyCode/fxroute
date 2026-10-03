/* SPDX-License-Identifier: AGPL-3.0-only */
#include <spa/support/plugin.h>
#include <spa/support/log.h>

extern const struct spa_handle_factory spa_audioconvert_factory;
static struct spa_handle_factory factory;
SPA_LOG_TOPIC_ENUM_DEFINE_REGISTERED;

SPA_EXPORT int spa_handle_factory_enum(const struct spa_handle_factory **result,uint32_t *index)
{
    if(*index)return 0;
    factory=spa_audioconvert_factory;
    factory.name="fxroute.src.convert";
    *result=&factory;
    ++*index;
    return 1;
}
