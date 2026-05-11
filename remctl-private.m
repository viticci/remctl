#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>

@interface REMObjectID : NSObject
+ (id)objectIDWithURL:(NSURL *)url;
- (NSUUID *)uuid;
- (NSURL *)urlRepresentation;
@end

@interface REMStore : NSObject
- (id)fetchReminderWithObjectID:(id)objectID error:(NSError **)error;
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

static BOOL looksLikeWebURL(NSString *value) {
    NSURL *url = [NSURL URLWithString:value];
    if (!url || url.host.length == 0) {
        return NO;
    }
    NSString *scheme = [url.scheme lowercaseString];
    return [scheme isEqualToString:@"http"] || [scheme isEqualToString:@"https"];
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
        ]];
        if (![action isKindOfClass:[NSString class]] || ![allowedActions containsObject:action]) {
            fail(@"Unknown action");
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
            NSArray<NSString *> *titles = stringArray(cmd[@"titles"], @"titles");
            if (titles.count == 0) fail(@"At least one subtask title is required");
            id subtaskContext = [change subtaskContext];
            NSMutableArray *subtaskURLs = [NSMutableArray array];
            for (NSString *title in titles) {
                id subtask = [save addReminderWithTitle:title toReminderSubtaskContextChangeItem:subtaskContext];
                id subtaskID = [subtask remObjectID];
                if (subtaskID) [subtaskURLs addObject:[[subtaskID urlRepresentation] absoluteString] ?: @""];
                addedSubtasks += 1;
            }
            details[@"subtaskURLs"] = subtaskURLs;
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
