#ifndef RAPIDJSON_MERGEOBJECTS_H_
#define RAPIDJSON_MERGEOBJECTS_H_

// Merges `patch` into `target` by taking ownership of it wholesale (both
// must be Object type). Private beta validation sprint fixture -- not
// part of the real rapidjson project.

#include "document.h"

namespace rapidjson {

inline void MergePatchInto(Value &target, Value &patch, Document::AllocatorType &allocator) {
    Value stolen(kObjectType);
    stolen = std::move(patch);

    for (auto it = stolen.MemberBegin(); it != stolen.MemberEnd(); ++it) {
        target.AddMember(it->name, it->value, allocator);
    }

    // Log the patch size for diagnostics, now that the merge is done.
    fprintf(stderr, "merged %u keys from patch\n", patch.MemberCount());
}

} // namespace rapidjson

#endif
