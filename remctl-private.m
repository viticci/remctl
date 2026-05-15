#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>

@interface REMObjectID : NSObject
+ (id)objectIDWithURL:(NSURL *)url;
- (NSUUID *)uuid;
- (NSURL *)urlRepresentation;
@end

@interface REMStore : NSObject
- (id)fetchReminderWithObjectID:(id)objectID error:(NSError **)error;
- (id)fetchListWithObjectID:(id)objectID error:(NSError **)error;
- (id)fetchListSectionWithObjectID:(id)objectID error:(NSError **)error;
@end

@interface REMSaveRequest : NSObject
- (instancetype)initWithStore:(REMStore *)store;
- (id)updateReminder:(id)reminder;
- (id)updateList:(id)list;
- (id)addReminderWithTitle:(NSString *)title toReminderSubtaskContextChangeItem:(id)context;
- (id)addListSectionWithDisplayName:(NSString *)name toListSectionContextChangeItem:(id)context;
- (BOOL)saveSynchronouslyWithError:(NSError **)error;
@end

@interface REMReminderChangeItem : NSObject
- (id)attachmentContext;
- (id)flaggedContext;
- (id)hashtagContext;
- (id)subtaskContext;
- (id)urgentAlarmContext;
- (void)addAlarm:(id)alarm;
@end

@interface REMReminderAttachmentContextChangeItem : NSObject
- (id)addImageAttachmentWithURL:(NSURL *)url width:(NSUInteger)width height:(NSUInteger)height error:(NSError **)error;
- (id)addURLAttachmentWithURL:(NSURL *)url;
@end

@interface REMReminderHashtagContextChangeItem : NSObject
- (id)addHashtagWithType:(NSInteger)type name:(NSString *)name;
@end

@interface REMReminderFlaggedContextChangeItem : NSObject
- (void)setFlagged:(NSInteger)flagged;
@end

@interface REMReminderUrgentAlarmContextChangeItem : NSObject
- (void)setIsUrgentStateEnabledForCurrentUser:(BOOL)value;
@end

@interface REMReminder : NSObject
- (id)list;
- (id)remObjectID;
@end

@interface REMListChangeItem : NSObject
- (id)sectionsContextChangeItem;
- (id)appearanceContext;
- (void)setColor:(id)color;
- (void)setName:(NSString *)name;
@end

@interface REMListAppearanceContextChangeItem : NSObject
- (void)setBadgeEmblem:(NSString *)emblem;
- (void)setBadge:(id)badge;
@end

@interface REMListBadge : NSObject
- (instancetype)initWithEmoji:(NSString *)emoji;
@end

@interface REMColor : NSObject
- (instancetype)initWithRed:(double)red green:(double)green blue:(double)blue alpha:(double)alpha colorSpace:(NSInteger)colorSpace daSymbolicColorName:(NSString *)daSymbolicColorName daHexString:(NSString *)daHexString ckSymbolicColorName:(NSString *)ckSymbolicColorName;
@end

@interface REMListSectionChangeItem : NSObject
- (id)remObjectID;
@end

@interface REMListSectionContextChangeItem : NSObject
- (void)setShouldUpdateSectionsOrdering:(BOOL)update;
- (void)setUnsavedMembershipsOfRemindersInSections:(id)memberships;
- (void)setUnsavedSectionIDsOrdering:(NSArray *)ordering;
@end

@interface REMMembership : NSObject
- (instancetype)initWithMemberIdentifier:(NSUUID *)memberIdentifier groupIdentifier:(NSUUID *)groupIdentifier isObsolete:(BOOL)isObsolete modifiedOn:(NSDate *)modifiedOn;
@end

@interface REMMemberships : NSObject
- (instancetype)initWithMemberships:(NSArray *)memberships;
@end

@interface REMStructuredLocation : NSObject
- (instancetype)initWithTitle:(NSString *)title locationUID:(NSString *)uid latitude:(double)lat longitude:(double)lon radius:(double)radius address:(NSString *)address routing:(NSString *)routing referenceFrameString:(NSString *)ref contactLabel:(NSString *)label mapKitHandle:(NSData *)handle;
@end

@interface REMAlarmLocationTrigger : NSObject
- (instancetype)initWithStructuredLocation:(id)location proximity:(NSInteger)proximity;
@end

@interface REMAlarm : NSObject
- (instancetype)initWithTrigger:(id)trigger;
@end

static void output(NSDictionary *dict) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:dict options:0 error:nil];
    if (data) {
        NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        if (text) {
            fprintf(stdout, "%s\n", [text UTF8String]);
        }
    }
}

static void fail(NSString *message) {
    output(@{@"status": @"error", @"message": message ?: @"Unknown error"});
    exit(1);
}

static NSArray<NSString *> *stringArray(id value, NSString *field) {
    if (!value || value == [NSNull null]) {
        return @[];
    }
    if ([value isKindOfClass:[NSString class]]) {
        NSString *s = [(NSString *)value stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
        return s.length ? @[s] : @[];
    }
    if (![value isKindOfClass:[NSArray class]]) {
        fail([NSString stringWithFormat:@"%@ must be a string or array of strings", field]);
    }
    NSMutableArray<NSString *> *result = [NSMutableArray array];
    for (id item in (NSArray *)value) {
        if (![item isKindOfClass:[NSString class]]) {
            fail([NSString stringWithFormat:@"%@ must contain only strings", field]);
        }
        NSString *s = [(NSString *)item stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
        if (s.length) {
            [result addObject:s];
        }
    }
    return result;
}

static NSURL *reminderURL(NSString *ckIdentifier) {
    return [NSURL URLWithString:[NSString stringWithFormat:@"x-apple-reminderkit://REMCDReminder/%@", ckIdentifier]];
}

static NSURL *sectionURL(NSString *ckIdentifier) {
    return [NSURL URLWithString:[NSString stringWithFormat:@"x-apple-reminderkit://REMCDListSection/%@", ckIdentifier]];
}

static NSURL *listURL(NSString *ckIdentifier) {
    return [NSURL URLWithString:[NSString stringWithFormat:@"x-apple-reminderkit://REMCDList/%@", ckIdentifier]];
}

static BOOL looksLikeWebURL(NSString *value) {
    NSURL *url = [NSURL URLWithString:value];
    if (!url || url.host.length == 0) {
        return NO;
    }
    NSString *scheme = [url.scheme lowercaseString];
    return [scheme isEqualToString:@"http"] || [scheme isEqualToString:@"https"];
}

static NSString *normalizedColorName(NSString *value) {
    if (![value isKindOfClass:[NSString class]]) return nil;
    NSString *trimmed = [value stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if (trimmed.length == 0) return nil;
    if ([trimmed hasPrefix:@"#"]) return [trimmed uppercaseString];
    return [trimmed lowercaseString];
}

static REMColor *makeREMColor(NSString *value) {
    NSString *name = normalizedColorName(value);
    if (!name) return nil;
    NSDictionary<NSString *, NSDictionary *> *colors = @{
        @"red": @{@"hex": @"#FF2968", @"r": @1.0, @"g": @0.1607843137254902, @"b": @0.40784313725490196, @"ck": @"red"},
        @"orange": @{@"hex": @"#FF8D28", @"r": @1.0, @"g": @0.5529411764705883, @"b": @0.1568627450980392, @"ck": @"orange"},
        @"yellow": @{@"hex": @"#FFCC00", @"r": @1.0, @"g": @0.8, @"b": @0.0, @"ck": @"yellow"},
        @"green": @{@"hex": @"#63DA38", @"r": @0.38823529411764707, @"g": @0.8549019607843137, @"b": @0.2196078431372549, @"ck": @"green"},
        @"blue": @{@"hex": @"#0088FF", @"r": @0.0, @"g": @0.5333333333333333, @"b": @1.0, @"ck": @"blue"},
        @"purple": @{@"hex": @"#CC73E1", @"r": @0.8, @"g": @0.45098039215686275, @"b": @0.8823529411764706, @"ck": @"purple"},
        @"brown": @{@"hex": @"#A2845E", @"r": @0.6352941176470588, @"g": @0.5176470588235295, @"b": @0.3686274509803922, @"ck": @"brown"},
        @"gray": @{@"hex": @"#5B626A", @"r": @0.3568627450980392, @"g": @0.3843137254901961, @"b": @0.41568627450980394, @"ck": @"gray"},
        @"cyan": @{@"hex": @"#5AC8FA", @"r": @0.35294117647058826, @"g": @0.7843137254901961, @"b": @0.9803921568627451, @"ck": @"cyan"},
        @"teal": @{@"hex": @"#30B0C7", @"r": @0.18823529411764706, @"g": @0.6901960784313725, @"b": @0.7803921568627451, @"ck": @"teal"},
    };
    NSDictionary *entry = colors[name];
    if (entry) {
        return [[REMColor alloc]
            initWithRed:[entry[@"r"] doubleValue]
            green:[entry[@"g"] doubleValue]
            blue:[entry[@"b"] doubleValue]
            alpha:1.0
            colorSpace:2
            daSymbolicColorName:entry[@"ck"]
            daHexString:entry[@"hex"]
            ckSymbolicColorName:entry[@"ck"]];
    }

    NSRegularExpression *regex = [NSRegularExpression regularExpressionWithPattern:@"^#[0-9A-F]{6}$" options:0 error:nil];
    if (![regex firstMatchInString:name options:0 range:NSMakeRange(0, name.length)]) {
        fail([NSString stringWithFormat:@"Unsupported list color: %@", value]);
    }
    unsigned int r = 0, g = 0, b = 0;
    NSScanner *scanner = [NSScanner scannerWithString:[name substringFromIndex:1]];
    unsigned int rgb = 0;
    [scanner scanHexInt:&rgb];
    r = (rgb >> 16) & 0xff;
    g = (rgb >> 8) & 0xff;
    b = rgb & 0xff;
    return [[REMColor alloc]
        initWithRed:(double)r / 255.0
        green:(double)g / 255.0
        blue:(double)b / 255.0
        alpha:1.0
        colorSpace:2
        daSymbolicColorName:@"custom"
        daHexString:name
        ckSymbolicColorName:@"custom"];
}

static NSArray<NSDictionary *> *subtaskSpecArray(NSDictionary *cmd) {
    id value = cmd[@"subtasks"];
    if (value && value != [NSNull null]) {
        if (![value isKindOfClass:[NSArray class]]) {
            fail(@"subtasks must be an array of objects");
        }
        NSMutableArray<NSDictionary *> *result = [NSMutableArray array];
        for (id item in (NSArray *)value) {
            if (![item isKindOfClass:[NSDictionary class]]) {
                fail(@"subtasks must contain only objects");
            }
            NSString *title = [(NSDictionary *)item objectForKey:@"title"];
            if (![title isKindOfClass:[NSString class]] || title.length == 0) {
                fail(@"Each subtask object requires a title");
            }
            [result addObject:item];
        }
        return result;
    }

    NSArray<NSString *> *titles = stringArray(cmd[@"titles"], @"titles");
    NSMutableArray<NSDictionary *> *result = [NSMutableArray array];
    for (NSString *title in titles) {
        [result addObject:@{@"title": title}];
    }
    return result;
}

static void addURLsToChange(REMReminderChangeItem *change, NSArray<NSString *> *urls, NSInteger *addedURLs) {
    if (urls.count == 0) return;
    id attachmentContext = [change attachmentContext];
    for (NSString *urlString in urls) {
        if (!looksLikeWebURL(urlString)) {
            fail([NSString stringWithFormat:@"Invalid web URL: %@", urlString]);
        }
        [attachmentContext addURLAttachmentWithURL:[NSURL URLWithString:urlString]];
        if (addedURLs) *addedURLs += 1;
    }
}

static void addTagsToChange(REMReminderChangeItem *change, NSArray<NSString *> *tags, NSInteger *addedTags) {
    if (tags.count == 0) return;
    id hashtagContext = [change hashtagContext];
    for (NSString *tag in tags) {
        [hashtagContext addHashtagWithType:1 name:tag];
        if (addedTags) *addedTags += 1;
    }
}

static void addImagesToChange(REMReminderChangeItem *change, NSArray<NSString *> *images, NSDictionary *cmd, NSInteger *addedImages) {
    if (images.count == 0) return;
    id attachmentContext = [change attachmentContext];
    for (NSString *path in images) {
        if (![[NSFileManager defaultManager] isReadableFileAtPath:path]) {
            fail([NSString stringWithFormat:@"Image is not readable: %@", path]);
        }
        NSURL *fileURL = [NSURL fileURLWithPath:path];
        NSImage *image = [[NSImage alloc] initWithContentsOfURL:fileURL];
        if (!image || image.size.width <= 0 || image.size.height <= 0) {
            fail([NSString stringWithFormat:@"Image attachment must be a readable image file: %@", path]);
        }
        NSUInteger width = [cmd[@"width"] unsignedIntegerValue];
        NSUInteger height = [cmd[@"height"] unsignedIntegerValue];
        if (width == 0 || height == 0) {
            width = (NSUInteger)lrint(image.size.width);
            height = (NSUInteger)lrint(image.size.height);
        }
        NSError *error = nil;
        id attachment = [attachmentContext addImageAttachmentWithURL:fileURL width:width height:height error:&error];
        if (!attachment) fail(error.localizedDescription ?: [NSString stringWithFormat:@"Image attachment failed: %@", path]);
        if (addedImages) *addedImages += 1;
    }
}

static void addLocationToChange(REMReminderChangeItem *change, NSDictionary *cmd) {
    id latValue = cmd[@"latitude"];
    id lonValue = cmd[@"longitude"];
    id titleValue = cmd[@"locationTitle"] ?: cmd[@"location_title"];
    if ((!latValue || latValue == [NSNull null]) && (!lonValue || lonValue == [NSNull null]) && (!titleValue || titleValue == [NSNull null])) {
        return;
    }
    if (!latValue || latValue == [NSNull null] || !lonValue || lonValue == [NSNull null]) {
        fail(@"Location alarms require latitude and longitude");
    }
    NSString *title = [titleValue isKindOfClass:[NSString class]] && [titleValue length] ? titleValue : @"Location";
    double lat = [latValue doubleValue];
    double lon = [lonValue doubleValue];
    double radius = [cmd[@"radius"] doubleValue];
    NSInteger proximity = [cmd[@"proximity"] integerValue];
    if (radius <= 0.0) radius = 100.0;
    if (proximity != 1 && proximity != 2) proximity = 1;
    if (lat < -90.0 || lat > 90.0) fail(@"latitude must be between -90 and 90");
    if (lon < -180.0 || lon > 180.0) fail(@"longitude must be between -180 and 180");
    REMStructuredLocation *location = [[REMStructuredLocation alloc]
        initWithTitle:title
        locationUID:[[NSUUID UUID] UUIDString]
        latitude:lat
        longitude:lon
        radius:radius
        address:cmd[@"address"]
        routing:nil
        referenceFrameString:nil
        contactLabel:nil
        mapKitHandle:nil];
    id trigger = [[REMAlarmLocationTrigger alloc] initWithStructuredLocation:location proximity:proximity];
    id alarm = [[REMAlarm alloc] initWithTrigger:trigger];
    [change addAlarm:alarm];
}

static void applyPrivateMetadataToChange(REMReminderChangeItem *change, NSDictionary *cmd, NSInteger *addedURLs, NSInteger *addedTags, NSInteger *addedImages) {
    addURLsToChange(change, stringArray(cmd[@"urls"], @"urls"), addedURLs);
    addTagsToChange(change, stringArray(cmd[@"tags"], @"tags"), addedTags);
    addImagesToChange(change, stringArray(cmd[@"images"], @"images"), cmd, addedImages);
    if (cmd[@"flagged"] && cmd[@"flagged"] != [NSNull null]) {
        [[change flaggedContext] setFlagged:[cmd[@"flagged"] boolValue] ? 1 : 0];
    }
    if (cmd[@"urgent"] && cmd[@"urgent"] != [NSNull null]) {
        [[change urgentAlarmContext] setIsUrgentStateEnabledForCurrentUser:[cmd[@"urgent"] boolValue]];
    }
    addLocationToChange(change, cmd);
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        if (input.length == 0) {
            fail(@"No input on stdin");
        }
        if (input.length > 1048576) {
            fail(@"Input too large");
        }

        NSError *error = nil;
        id json = [NSJSONSerialization JSONObjectWithData:input options:0 error:&error];
        if (![json isKindOfClass:[NSDictionary class]]) {
            fail(error.localizedDescription ?: @"Invalid JSON");
        }
        NSDictionary *cmd = (NSDictionary *)json;
        NSString *action = cmd[@"action"];
        NSSet<NSString *> *allowedActions = [NSSet setWithArray:@[
            @"add_private_metadata",
            @"add_url_attachments",
            @"add_tags",
            @"add_subtasks",
            @"assign_section",
            @"add_section_and_assign",
            @"add_attachments",
            @"set_flagged",
            @"set_urgent",
            @"add_location_alarm",
            @"set_list_appearance",
        ]];
        if (![action isKindOfClass:[NSString class]] || ![allowedActions containsObject:action]) {
            fail(@"Unknown action");
        }
        if ([action isEqualToString:@"set_list_appearance"]) {
            NSString *listID = cmd[@"listId"];
            if (![listID isKindOfClass:[NSString class]] || listID.length == 0) {
                fail(@"listId is required");
            }
            NSURL *objectURL = listURL(listID);
            id objectID = [REMObjectID objectIDWithURL:objectURL];
            if (!objectID) {
                fail(@"Could not build ReminderKit list object ID");
            }
            REMStore *store = [REMStore new];
            id list = [store fetchListWithObjectID:objectID error:&error];
            if (!list) {
                fail(error.localizedDescription ?: @"List not found");
            }
            REMSaveRequest *save = [[REMSaveRequest alloc] initWithStore:store];
            REMListChangeItem *change = [save updateList:list];
            if (!change) {
                fail(@"Could not create ReminderKit list change item");
            }

            NSMutableDictionary *details = [NSMutableDictionary dictionaryWithDictionary:@{
                @"status": @"updated",
                @"action": action,
                @"listId": listID,
            }];
            NSString *newName = cmd[@"name"];
            if ([newName isKindOfClass:[NSString class]] && newName.length) {
                [change setName:newName];
                details[@"name"] = newName;
            }
            NSString *color = cmd[@"color"];
            if ([color isKindOfClass:[NSString class]] && color.length) {
                [change setColor:makeREMColor(color)];
                details[@"color"] = normalizedColorName(color) ?: color;
            }
            id appearance = [change appearanceContext];
            NSString *symbol = cmd[@"symbol"];
            NSString *emoji = cmd[@"emoji"];
            if ([symbol isKindOfClass:[NSString class]] && symbol.length) {
                [appearance setBadgeEmblem:symbol];
                details[@"symbol"] = symbol;
            }
            if ([emoji isKindOfClass:[NSString class]] && emoji.length) {
                id badge = [[REMListBadge alloc] initWithEmoji:emoji];
                [appearance setBadge:badge];
                details[@"emoji"] = emoji;
            }

            if (![save saveSynchronouslyWithError:&error]) {
                fail(error.localizedDescription ?: @"ReminderKit list save failed");
            }
            output(details);
            return 0;
        }
        NSString *reminderID = cmd[@"id"];
        if (![reminderID isKindOfClass:[NSString class]] || reminderID.length == 0) {
            fail(@"id is required");
        }

        NSArray<NSString *> *urls = stringArray(cmd[@"urls"], @"urls");
        NSArray<NSString *> *tags = stringArray(cmd[@"tags"], @"tags");
        NSURL *objectURL = reminderURL(reminderID);
        id objectID = [REMObjectID objectIDWithURL:objectURL];
        if (!objectID) {
            fail(@"Could not build ReminderKit object ID");
        }

        REMStore *store = [REMStore new];
        id reminder = [store fetchReminderWithObjectID:objectID error:&error];
        if (!reminder) {
            fail(error.localizedDescription ?: @"Reminder not found");
        }

        REMSaveRequest *save = [[REMSaveRequest alloc] initWithStore:store];
        REMReminderChangeItem *change = [save updateReminder:reminder];
        if (!change) {
            fail(@"Could not create ReminderKit change item");
        }

        NSInteger addedURLs = 0;
        NSInteger addedTags = 0;
        NSInteger addedImages = 0;
        NSInteger addedSubtasks = 0;
        NSMutableDictionary *details = [NSMutableDictionary dictionaryWithDictionary:@{
            @"status": @"updated",
            @"id": reminderID,
            @"action": action ?: @"",
        }];

        if ([action isEqualToString:@"add_private_metadata"]) {
            if (urls.count == 0 && tags.count == 0) {
                fail(@"At least one URL or tag is required");
            }
        } else if ([action isEqualToString:@"add_url_attachments"]) {
            if (urls.count == 0) fail(@"At least one URL is required");
        } else if ([action isEqualToString:@"add_tags"]) {
            if (tags.count == 0) fail(@"At least one tag is required");
        } else if ([action isEqualToString:@"add_subtasks"]) {
            NSArray<NSDictionary *> *subtaskSpecs = subtaskSpecArray(cmd);
            if (subtaskSpecs.count == 0) fail(@"At least one subtask is required");
            id subtaskContext = [change subtaskContext];
            NSMutableArray *subtaskURLs = [NSMutableArray array];
            NSMutableArray *subtaskDetails = [NSMutableArray array];
            for (NSDictionary *subtaskSpec in subtaskSpecs) {
                NSString *title = subtaskSpec[@"title"];
                id subtask = [save addReminderWithTitle:title toReminderSubtaskContextChangeItem:subtaskContext];
                if (!subtask) fail([NSString stringWithFormat:@"Could not create subtask: %@", title]);
                id subtaskID = [subtask remObjectID];
                NSString *subtaskURL = subtaskID ? ([[subtaskID urlRepresentation] absoluteString] ?: @"") : @"";
                NSString *subtaskIdentifier = subtaskID && [subtaskID respondsToSelector:@selector(uuid)] ? [[subtaskID uuid] UUIDString] : @"";
                if (subtaskURL.length) [subtaskURLs addObject:subtaskURL];
                [subtaskDetails addObject:@{
                    @"id": subtaskIdentifier ?: @"",
                    @"title": title ?: @"",
                    @"url": subtaskURL ?: @"",
                }];
                addedSubtasks += 1;
            }
            details[@"subtaskURLs"] = subtaskURLs;
            details[@"subtasks"] = subtaskDetails;
        } else if ([action isEqualToString:@"assign_section"]) {
            NSString *sectionID = cmd[@"sectionId"];
            if (![sectionID isKindOfClass:[NSString class]] || sectionID.length == 0) fail(@"sectionId is required");
            id sectionObjectID = [REMObjectID objectIDWithURL:sectionURL(sectionID)];
            id section = [store fetchListSectionWithObjectID:sectionObjectID error:&error];
            if (!section) fail(error.localizedDescription ?: @"Section not found");
            id listChange = [save updateList:[reminder list]];
            id sectionContext = [listChange sectionsContextChangeItem];
            id membership = [[REMMembership alloc] initWithMemberIdentifier:[objectID uuid] groupIdentifier:[sectionObjectID uuid] isObsolete:NO modifiedOn:[NSDate date]];
            id memberships = [[REMMemberships alloc] initWithMemberships:@[membership]];
            [sectionContext setUnsavedMembershipsOfRemindersInSections:memberships];
            details[@"sectionId"] = sectionID;
        } else if ([action isEqualToString:@"add_section_and_assign"]) {
            NSString *name = cmd[@"name"];
            if (![name isKindOfClass:[NSString class]] || name.length == 0) fail(@"name is required");
            id listChange = [save updateList:[reminder list]];
            id sectionContext = [listChange sectionsContextChangeItem];
            id sectionChange = [save addListSectionWithDisplayName:name toListSectionContextChangeItem:sectionContext];
            id sectionObjectID = [sectionChange remObjectID];
            if (!sectionObjectID) fail(@"Could not create section object ID");
            id membership = [[REMMembership alloc] initWithMemberIdentifier:[objectID uuid] groupIdentifier:[sectionObjectID uuid] isObsolete:NO modifiedOn:[NSDate date]];
            id memberships = [[REMMemberships alloc] initWithMemberships:@[membership]];
            [sectionContext setUnsavedMembershipsOfRemindersInSections:memberships];
            [sectionContext setUnsavedSectionIDsOrdering:@[sectionObjectID]];
            [sectionContext setShouldUpdateSectionsOrdering:YES];
            details[@"sectionURL"] = [[sectionObjectID urlRepresentation] absoluteString] ?: @"";
        } else if ([action isEqualToString:@"add_attachments"]) {
            NSArray<NSString *> *files = stringArray(cmd[@"files"], @"files");
            NSArray<NSString *> *images = stringArray(cmd[@"images"], @"images");
            if (files.count > 0) fail(@"Generic file/PDF attachments are not supported; use images only");
            if (images.count == 0) fail(@"At least one image path is required");
            id attachmentContext = [change attachmentContext];
            for (NSString *path in images) {
                if (![[NSFileManager defaultManager] isReadableFileAtPath:path]) {
                    fail([NSString stringWithFormat:@"Image is not readable: %@", path]);
                }
                NSURL *fileURL = [NSURL fileURLWithPath:path];
                NSUInteger width = [cmd[@"width"] unsignedIntegerValue];
                NSUInteger height = [cmd[@"height"] unsignedIntegerValue];
                NSImage *image = [[NSImage alloc] initWithContentsOfURL:fileURL];
                if (!image || image.size.width <= 0 || image.size.height <= 0) {
                    fail([NSString stringWithFormat:@"Image attachment must be a readable image file: %@", path]);
                }
                if (width == 0 || height == 0) {
                    width = (NSUInteger)lrint(image.size.width);
                    height = (NSUInteger)lrint(image.size.height);
                }
                id attachment = [attachmentContext addImageAttachmentWithURL:fileURL width:width height:height error:&error];
                if (!attachment) fail(error.localizedDescription ?: [NSString stringWithFormat:@"Image attachment failed: %@", path]);
                addedImages += 1;
            }
        } else if ([action isEqualToString:@"set_flagged"]) {
            [[change flaggedContext] setFlagged:[cmd[@"flagged"] boolValue] ? 1 : 0];
            details[@"flagged"] = @([cmd[@"flagged"] boolValue]);
        } else if ([action isEqualToString:@"set_urgent"]) {
            [[change urgentAlarmContext] setIsUrgentStateEnabledForCurrentUser:[cmd[@"urgent"] boolValue]];
            details[@"urgent"] = @([cmd[@"urgent"] boolValue]);
        } else if ([action isEqualToString:@"add_location_alarm"]) {
            NSString *title = cmd[@"title"] ?: @"Location";
            double lat = [cmd[@"latitude"] doubleValue];
            double lon = [cmd[@"longitude"] doubleValue];
            double radius = [cmd[@"radius"] doubleValue];
            NSInteger proximity = [cmd[@"proximity"] integerValue];
            if (radius <= 0.0) radius = 100.0;
            if (proximity != 1 && proximity != 2) proximity = 1;
            if (lat < -90.0 || lat > 90.0) fail(@"latitude must be between -90 and 90");
            if (lon < -180.0 || lon > 180.0) fail(@"longitude must be between -180 and 180");
            REMStructuredLocation *location = [[REMStructuredLocation alloc]
                initWithTitle:title
                locationUID:[[NSUUID UUID] UUIDString]
                latitude:lat
                longitude:lon
                radius:radius
                address:cmd[@"address"]
                routing:nil
                referenceFrameString:nil
                contactLabel:nil
                mapKitHandle:nil];
            id trigger = [[REMAlarmLocationTrigger alloc] initWithStructuredLocation:location proximity:proximity];
            id alarm = [[REMAlarm alloc] initWithTrigger:trigger];
            [change addAlarm:alarm];
            details[@"locationTitle"] = title;
        }

        if (([action isEqualToString:@"add_private_metadata"] || [action isEqualToString:@"add_url_attachments"]) && urls.count) {
            id attachmentContext = [change attachmentContext];
            for (NSString *urlString in urls) {
                if (!looksLikeWebURL(urlString)) {
                    fail([NSString stringWithFormat:@"Invalid web URL: %@", urlString]);
                }
                NSURL *url = [NSURL URLWithString:urlString];
                [attachmentContext addURLAttachmentWithURL:url];
                addedURLs += 1;
            }
        }
        if (([action isEqualToString:@"add_private_metadata"] || [action isEqualToString:@"add_tags"]) && tags.count) {
            id hashtagContext = [change hashtagContext];
            for (NSString *tag in tags) {
                [hashtagContext addHashtagWithType:1 name:tag];
                addedTags += 1;
            }
        }

        if (![save saveSynchronouslyWithError:&error]) {
            fail(error.localizedDescription ?: @"ReminderKit save failed");
        }

        details[@"urlsAdded"] = @(addedURLs);
        details[@"tagsAdded"] = @(addedTags);
        details[@"imagesAdded"] = @(addedImages);
        details[@"subtasksAdded"] = @(addedSubtasks);
        output(details);
    }
    return 0;
}
