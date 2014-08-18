from askbot.utils import decorators
from askbot import const
from askbot import models
from askbot import mail
from datetime import datetime
from django.utils.translation import string_concat
from django.utils.translation import ungettext
from django.utils.translation import ugettext as _
from django.template.loader import get_template
from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.encoding import force_text
from django.template import RequestContext
from django.views.decorators import csrf
from django.utils.encoding import force_text
from django.core import exceptions
from django.utils import simplejson

EDIT_ACTIVITY_TYPES = (
    const.TYPE_ACTIVITY_MODERATED_NEW_POST,
    const.TYPE_ACTIVITY_MODERATED_POST_EDIT
)
MOD_ACTIVITY_TYPES = EDIT_ACTIVITY_TYPES + (const.TYPE_ACTIVITY_MARK_OFFENSIVE,)

#some utility functions
def get_object(memo):
    content_object = memo.activity.content_object
    if isinstance(content_object, models.PostRevision):
        return content_object.post
    else:
        return content_object


def get_editors(memo_set):
    """returns editors corresponding to the memo set
    some memos won't yeild editors - if the related object
    is post and it has > 1 editor (in which case we don't know
    who was the editor that we want to block!!! 
    this applies to flagged posts.

    todo: an inconvenience is that "offensive flags" are stored
    differently in the Activity vs. "new moderated posts" or "post edits"
    """
    editors = set()
    for memo in memo_set:
        obj = memo.activity.content_object
        if isinstance(obj, models.PostRevision):
            editors.add(obj.author)
        elif isinstance(obj, models.Post):
            rev_authors = set()
            for rev in obj.revisions.all():
                rev_authors.add(rev.author)

            #if we have > 1 author we skip, b/c don't know
            #which user we want to block
            if len(rev_authors) == 1:
                editors.update(rev_authors)
    return editors

def filter_admins(users):
    filtered = set()
    for user in users:
        if not user.is_administrator_or_moderator():
            filtered.add(user)
    return filtered
            

def concat_messages(message1, message2):
    if message1:
        message = string_concat(message1, ', ')
        return string_concat(message, message2)
    else:
        return message2


@csrf.csrf_exempt
@decorators.post_only
@decorators.ajax_only
def moderate_post_edits(request):
    if request.user.is_anonymous():
        raise exceptions.PermissionDenied()
    if not request.user.is_administrator_or_moderator():
        raise exceptions.PermissionDenied()

    post_data = simplejson.loads(request.raw_post_data)
    #{'action': 'decline-with-reason', 'items': ['posts'], 'reason': 1, 'edit_ids': [827]}

    memo_set = models.ActivityAuditStatus.objects.filter(id__in=post_data['edit_ids'])
    result = {
        'message': '',
        'memo_ids': set()
    }

    #if we are approving or declining users we need to expand the memo_set
    #to all of their edits of those users
    if post_data['action'] in ('block', 'approve') and 'users' in post_data['items']:
        editors = filter_admins(get_editors(memo_set))
        items = models.Activity.objects.filter(
                                activity_type__in=EDIT_ACTIVITY_TYPES,
                                user__in=editors
                            )
        memo_filter = Q(id__in=post_data['edit_ids']) | Q(user=request.user, activity__in=items)
        memo_set = models.ActivityAuditStatus.objects.filter(memo_filter)

    memo_set.select_related('activity')

    if post_data['action'] == 'decline-with-reason':
        #todo: bunch notifications - one per recipient
        num_posts = 0
        for memo in memo_set:
            post = get_object(memo)
            request.user.delete_post(post)
            reject_reason = models.PostFlagReason.objects.get(id=post_data['reason'])
            template = get_template('email/rejected_post.html')
            data = {
                    'post': post.html,
                    'reject_reason': reject_reason.details.html
                   }
            body_text = template.render(RequestContext(request, data))
            mail.send_mail(
                subject_line=_('your post was not accepted'),
                body_text=unicode(body_text),
                recipient=post.author
            )
            num_posts += 1

        #message to moderator
        if num_posts:
            posts_message = ungettext('%d post deleted', '%d posts deleted', num_posts) % num_posts
            result['message'] = concat_messages(result['message'], posts_message)

    elif post_data['action'] == 'approve':
        num_posts = 0
        if 'posts' in post_data['items']:
            for memo in memo_set:
                if memo.activity.activity_type == const.TYPE_ACTIVITY_MARK_OFFENSIVE:
                    #unflag the post
                    content_object = memo.activity.content_object
                    request.user.flag_post(content_object, cancel_all=True, force=True)
                    num_posts += 1
                else:
                    revision = memo.activity.content_object
                    if isinstance(revision, models.PostRevision):
                        request.user.approve_post_revision(revision)
                        num_posts += 1

            if num_posts > 0:
                posts_message = ungettext('%d post approved', '%d posts approved', num_posts) % num_posts
                result['message'] = concat_messages(result['message'], posts_message)

        if 'users' in post_data['items']:
            editors = filter_admins(get_editors(memo_set))
            assert(request.user not in editors)
            for editor in editors:
                editor.set_status('a')

            num_editors = len(editors)
            if num_editors:
                users_message = ungettext('%d user approved', '%d users approved', num_editors) % num_editors
                result['message'] = concat_messages(result['message'], users_message)
            
    elif post_data['action'] == 'block':
        if 'users' in post_data['items']:
            editors = filter_admins(get_editors(memo_set))
            assert(request.user not in editors)
            num_posts = 0
            for editor in editors:
                #block user
                editor.set_status('b')
                #delete all content by the user
                num_posts += request.user.delete_all_content_authored_by_user(editor)

            if num_posts:
                posts_message = ungettext('%d post deleted', '%d posts deleted', num_posts) % num_posts
                result['message'] = concat_messages(result['message'], posts_message)

            num_editors = len(editors)
            if num_editors:
                users_message = ungettext('%d user blocked', '%d users blocked', num_editors) % num_editors
                result['message'] = concat_messages(result['message'], users_message)

        moderate_ips = getattr(django_settings, 'ASKBOT_IP_MODERATION_ENABLED', False)
        if moderate_ips and 'ips' in post_data['items']:
            ips = set()
            for memo in memo_set:
                obj = memo.activity.content_object
                if isinstance(obj, models.PostRevision):
                    ips.add(obj.ip_addr)

            #to make sure to not block the admin and 
            #in case REMOTE_ADDR is a proxy server - not
            #block access to the site
            my_ip = request.META.get('REMOTE_ADDR')
            if my_ip in ips:
                ips.remove(my_ip)

            from stopforumspam.models import Cache
            already_blocked = Cache.objects.filter(ip__in=ips)
            already_blocked.update(permanent=True)
            already_blocked_ips = already_blocked.values_list('ip', flat=True)
            ips = ips - set(already_blocked_ips)
            for ip in ips:
                cache = Cache(ip=ip, permanent=True)
                cache.save()

            num_ips = len(ips)
            if num_ips:
                ips_message = ungettext('%d ip blocked', '%d ips blocked', num_ips) % num_ips
                result['message'] = concat_messages(result['message'], ips_message)

    result['memo_ids'] = [memo.id for memo in memo_set]#why values_list() fails here?
    result['message'] = force_text(result['message'])

    #delete items from the moderation queue
    act_ids = list(memo_set.values_list('activity_id', flat=True))
    acts = models.Activity.objects.filter(
                            id__in=act_ids,
                            activity_type__in=MOD_ACTIVITY_TYPES
                        )

    memos = models.ActivityAuditStatus.objects.filter(activity__id__in=act_ids)
    memos.delete()

    acts.delete()
    request.user.update_response_counts()
    result['memo_count'] = request.user.get_notifications(MOD_ACTIVITY_TYPES).count()
    return result
