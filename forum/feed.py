#!/usr/bin/env python
#encoding:utf-8
#-------------------------------------------------------------------------------
# Name:        Syndication feed class for subsribtion
# Purpose:
#
# Author:      Mike
#
# Created:     29/01/2009
# Copyright:   (c) CNPROG.COM 2009
# Licence:     GPL V2
#-------------------------------------------------------------------------------
from django.contrib.syndication.feeds import Feed, FeedDoesNotExist
from django.utils.translation import ugettext as _
from models import Question
import settings
class RssLastestQuestionsFeed(Feed):
    title = settings.APP_TITLE + _(' - ')+ _('latest questions')
    link = settings.APP_URL + '/' + _('questions/')
    description = settings.APP_DESCRIPTION
    #ttl = 10
    copyright = settings.APP_COPYRIGHT

    def item_link(self, item):
        return settings.APP_URL + '%s' % item.get_absolute_url()

    def item_author_name(self, item):
        return item.author.username

    def item_author_link(self, item):
        return item.author.get_profile_url()

    def item_pubdate(self, item):
        return item.added_at

    def items(self, item):
       return Question.objects.filter(deleted=False).order_by('-added_at')[:30]

def main():
    pass

if __name__ == '__main__':
    main()
