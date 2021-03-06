from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from datetime import date, datetime
import pytz
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect
from django.views.decorators.http import require_http_methods
from django.db.models import Count, Case, When, Q
from .models import Idea, Criterion, Popular_Vote, Phase, Phase_History, Category, Comment, UserProfile, Dimension, Evaluation, Category_Image, Use_Term, Challenge
from .forms import IdeaForm, CriterionForm, IdeaFormUpdate, CategoryForm, CategoryImageForm, EvaluationForm, ChallengeForm, UseTermForm
from .singleton import Profanity_Check
from django import forms
from wordfilter import Wordfilter
import os
import json
import uuid
from django.contrib import messages
from django.utils.translation import ugettext_lazy as _
from django.forms import modelformset_factory
import collections
from django.http import HttpResponse
from django.template.loader import get_template
from django.template import Context
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.decorators import permission_required
from django.core import mail
from django.core.mail import EmailMessage
from .mail_util import Mail_Util
import mistune
from .util import get_ip, get_client_ip
import logging
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import connection
import csv
from django.http import StreamingHttpResponse
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from martor.utils import LazyEncoder
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
# Creating log object
logger = logging.getLogger('audit_log')

mail_util = Mail_Util()

def audit(username, ip_addr, operation, className, objectId):
    logger.info(
        '%(username)s|%(ip_addr)s|%(operation)s|%(className)s|%(objectId)s',
        {
            'username': username,
            'ip_addr': ip_addr,
            'operation': operation,
            'className': className,
            'objectId': objectId
        }
    )

def index(request):
    if request.user.is_authenticated:
        audit(request.user.username, get_client_ip(request), 'LIST_IDEAS_PAGE', Idea.__name__, '')
        return idea_list(request)
    return render(request, 'ideax/index.html')

@login_required
def accept_use_term(request):
    if not request.user.userprofile.use_term_accept:
        user_profile = UserProfile.objects.get(user=request.user)
        user_profile.use_term_accept = True
        user_profile.acceptance_date = timezone.localtime(timezone.now())
        user_profile.ip = get_ip(request)
        user_profile.save()
        messages.success(request, _('Term of use accepted!'))
        #logger.info('%(username)s|%(ip_addr)s|%(message)s', { 'username': request.user.username, 'ip_addr': get_client_ip(request), 'message': 'Term of use accepted!'})
    else:
        messages.success(request, _('Term of use already accepted!'))
        #logger.info('%(username)s|%(ip_addr)s|%(message)s', { 'username': request.user.username, 'ip_addr': get_client_ip(request), 'message': 'Term of use already accepted!'})

    audit(request.user.username, get_client_ip(request), 'ACCEPT_TERMS_OF_USE_OPERATION', UserProfile.__name__, '')
    return redirect('index') 

@login_required
def idea_list(request):
    ideas = get_ideas_init(request)
    ideas['phases'] = get_phases_count()
    
    page = request.GET.get('page', 1)
    paginator = Paginator(ideas['ideas'], 5)

    try:
        ideas['ideas'] = paginator.page(page)
    except PageNotAnInteger:
        ideas['ideas'] = paginator.page(1)
    except EmptyPage:
        ideas['ideas'] = paginator.page(paginator.num_pages)
    return render(request, 'ideax/idea_list.html', ideas)


def get_phases_count():
    cursor = connection.cursor()
    cursor.execute('''select current_phase as phase, count(*) as qtd
                      from ideax_phase_history ph inner join ideax_idea i on ph.idea_id = i.id
                      where ph.current =1 and i.discarded = 0 group by current_phase order by phase''')
    data = cursor.fetchall()

    phases = dict()
    for i in Phase.choices():
        phases[i[0]] = {'phase': i[1], 'qtd': 0}


    for d in data:
        phases[d[0]]['qtd'] = d[1]

    return phases

@login_required
def get_ideas_init(request):
    ideas_dic = dict()
    ideas_dic['ideas'] = Idea.objects.filter(discarded=False, phase_history__current_phase=1, phase_history__current=1).annotate(count_like=Count(Case(When(popular_vote__like = True, then=1)))).order_by('-count_like')
    ideas_dic['ideas_liked'] = get_ideas_voted(request, True)
    ideas_dic['ideas_disliked'] = get_ideas_voted(request, False)
    ideas_dic['ideas_created_by_me'] = get_ideas_created(request)
    ideas_dic['challenges'] = get_featured_challenges(request)
    ideas_dic['phase_req'] = Phase.GROW.id
    return ideas_dic

def get_phases():
    phase_dic = dict()
    phase_dic['phases'] = Phase.choices()
    return phase_dic

def idea_filter(request, phase_pk=None, search_part=None):
    if phase_pk:
        ideas = Idea.objects.filter(discarded=False, phase_history__current_phase=phase_pk, phase_history__current=1).annotate(count_like=Count(Case(When(popular_vote__like = True, then=1)))).order_by('-count_like')
    else:
        ideas  = Idea.objects.filter(Q(authors__user__first_name__icontains=search_part)|Q(title__icontains=search_part)|Q(summary__icontains=search_part) , discarded=False).annotate(count_like=Count(Case(When(popular_vote__like = True, then=1)))).order_by('-count_like')
    
    context={'ideas': ideas,
             'ideas_liked': get_ideas_voted(request, True),
             'ideas_disliked': get_ideas_voted(request, False),
             'ideas_created_by_me' : get_ideas_created(request),}

    data = dict()
    if request.is_ajax():
        data['html_idea_list'] = render_to_string('ideax/idea_list_loop.html', context, request=request)
        data['empty'] = 0

        if not ideas:
            data['html_idea_list'] = render_to_string('ideax/includes/empty.html', request=request)
            data['empty'] = 1
        return JsonResponse(data)
    else:
        context['challenges'] = get_featured_challenges(request)
        context['phases'] = get_phases_count()
        context['phase_req'] = phase_pk
        return render(request, 'ideax/idea_list.html', context)

@login_required
def save_idea(request, form, template_name, new=False):
    data = dict()
    if request.method == "POST":
        if form.is_valid():
            idea = form.save(commit=False)
            
            if new:
                idea_autor = UserProfile.objects.get(user=request.user)
                idea.author = idea_autor
                idea.creation_date = timezone.now()
                idea.phase= Phase.GROW.id

                if(idea.challenge):
                    idea.category = idea.challenge.category
                    category_image = Category_Image.get_random_image(idea.challenge.category)
                else:
                    category_image = Category_Image.get_random_image(idea.category)

                if category_image:
                    idea.category_image = category_image.image.url

                idea.save()
                idea.authors.add(idea.author)
                phase_history = Phase_History(current_phase=Phase.GROW.id,
                                              previous_phase=0,
                                              date_change=timezone.now(),
                                              idea=idea,
                                              author=idea.author,
                                              current=True)
                phase_history.save()
            else:
                idea.save()

            idea.authors.clear()
            idea.authors.add(UserProfile.objects.get(user__email=request.user.email))
            if form.cleaned_data['authors']:
                for author in form.cleaned_data['authors']:
                    idea.authors.add(author)
                

            messages.success(request, _('Idea saved successfully!'))

            audit(request.user.username, get_client_ip(request), 'SAVE_IDEA_OPERATION', Idea.__name__, str(idea.id))

            return redirect('idea_list')

    return render(request, template_name, {'form': form})

@login_required
@permission_required('ideax.add_idea',raise_exception=True)
def idea_new(request):
    queryset = get_authors(request.user.email)
    if request.method == "POST":
        form = IdeaForm(request.POST,authors=queryset)
    else:
        form = IdeaForm(authors=queryset)

    audit(request.user.username, get_client_ip(request), 'CREATE_IDEA_FORM', Idea.__name__, '')

    return save_idea(request, form, 'ideax/idea_new.html', True)

@login_required
@permission_required('ideax.change_idea',raise_exception=True)
def idea_edit(request, pk):
    idea = get_object_or_404(Idea, pk=pk)

    if ((request.user.userprofile == idea.author and idea.get_current_phase() == Phase.GROW)
                    or request.user.has_perm(settings.PERMISSIONS["MANAGE_IDEA"])):
        queryset = get_authors(request.user.email)
        if request.method == "POST":
            form = IdeaForm(request.POST, instance=idea, authors=queryset)
        else:
            form = IdeaForm(instance=idea, authors=queryset)

        audit(request.user.username, get_client_ip(request), 'EDIT_IDEA_FORM', Idea.__name__, str(idea.id))

        return save_idea(request, form, 'ideax/idea_edit.html')
    else:
        messages.error(request, _('Not supported action'))
        return redirect('index')

@login_required
@permission_required('ideax.delete_idea',raise_exception=True)
def idea_remove(request, pk):
    idea = get_object_or_404(Idea, pk=pk)
    data = dict()

    if ((request.user.userprofile == idea.author and idea.get_current_phase() == Phase.GROW)
                    or request.user.has_perm(settings.PERMISSIONS["MANAGE_IDEA"])):
        if request.method == 'POST':
            idea.discarded = True
            idea.save()
            data['form_is_valid'] = True
            ideas = get_ideas_init(request)
            data['html_list'] = render_to_string('ideax/idea_list_loop.html', ideas, request=request)
        else:
            context = {'idea' : idea}
            data['html_form'] = render_to_string('ideax/includes/partial_idea_remove.html',
                                                 context,
                                                 request=request,)

        audit(request.user.username, get_client_ip(request), 'REMOVE_IDEA_CONFIRMATION', Idea.__name__, str(idea.id))

        return JsonResponse(data)
    else:
        messages.error(request, _('Not supported action'))
        return redirect('index')

@login_required
@permission_required('ideax.add_criterion',raise_exception=True)
def criterion_new(request):
    if request.method == "POST":
        form = CriterionForm(request.POST)
        if form.is_valid():
            criterion = form.save(commit=False)
            criterion.save()

            audit(request.user.username, get_client_ip(request), 'CREATE_NEW_CRITERION_OPERATION', Criterion.__name__, str(criterion.id))

            return redirect('criterion_list')
    else:
        form = CriterionForm()

    return render(request, 'ideax/criterion_edit.html', {'form': form})

@login_required
@permission_required('ideax.add_criterion',raise_exception=True)
def criterion_list(request):
    criterion = Criterion.objects.all()
    audit(request.user.username, get_client_ip(request), 'CRITERION_LIST', Criterion.__name__, '')
    return render(request, 'ideax/criterion_list.html', {'criterions': criterion})

@login_required
@permission_required('ideax.change_criterion',raise_exception=True)
def criterion_edit(request, pk):
    criterion = get_object_or_404(Criterion, pk=pk)
    if request.method == "POST":
        form = CriterionForm(request.POST, instance=criterion)
        if form.is_valid():
            criterion = form.save(commit=False)
            criterion.save()

            audit(request.user.username, get_client_ip(request), 'EDIT_CRITERION_SAVE', Criterion.__name__, str(criterion.id))

            return redirect('criterion_list')
    else:
        form = CriterionForm(instance=criterion)

    return render(request, 'ideax/criterion_edit.html', {'form': form})

@login_required
@permission_required('ideax.add_evaluation',raise_exception=True)
def idea_evaluation(request, idea_pk):
    valuator = UserProfile.objects.get(user=request.user)
    idea = get_object_or_404(Idea, pk=idea_pk)

    idea_score = 0.0
    divisor = 0
    soma = 0

    dim = Dimension.objects.filter(final_date__isnull=True)

    data = dict()
    if request.POST:
        form_ = EvaluationForm(request.POST, extra=dim)
        if form_.is_valid():
            for i in dim:
                divisor += i.weight
                dimension_value = i.weight * form_.cleaned_data[EvaluationForm.FORMAT_ID % i.pk].value
                soma += dimension_value
                if idea.score <= 0:
                    evaluation = Evaluation(valuator=valuator,
                                            idea=idea,
                                            dimension=i,
                                            category_dimension=form_.cleaned_data[EvaluationForm.FORMAT_ID % i.pk],
                                            evaluation_date=timezone.now(),
                                            dimension_value=dimension_value,
                                            note=form_.cleaned_data[EvaluationForm.FORMAT_ID_NOTE % i.pk],)
                else:
                    evaluation = Evaluation.objects.get(dimension=i, idea=idea)
                    evaluation.valuator = valuator
                    evaluation.evaluation_date = timezone.now()
                    evaluation.category_dimension = form_.cleaned_data[EvaluationForm.FORMAT_ID % i.pk]
                    evaluation.note = form_.cleaned_data[EvaluationForm.FORMAT_ID_NOTE % i.pk]
                    evaluation.dimension_value=dimension_value

                evaluation.save()

                audit(request.user.username, get_client_ip(request), 'CREATE_EVALUATION_SAVE', Evaluation.__name__, str(evaluation.id))

            idea_score = soma/divisor
            idea.score = idea_score
            idea.save()

            audit(request.user.username, get_client_ip(request), 'EDIT_IDEA_EVALUATION_SAVE', Idea.__name__, str(idea.id))

            data['score_value'] = idea_score
        else:
            data["msg"] = _("Something went wrong or you're not allowed!")
            return JsonResponse(data, status=500)
    else:
        form_ = EvaluationForm(extra=dim)

    data["msg"] = _("Evaluation done successfully!")
    return JsonResponse(data)

@login_required
@permission_required('ideax.delete_criterion',raise_exception=True)
def criterion_remove(request, pk):
    criterion = get_object_or_404(Criterion, pk=pk)

    criterion.delete()

    audit(request.user.username, get_client_ip(request), 'REMOVE_CRITERION_SAVE', Criterion.__name__, str(pk))

    return redirect('criterion_list')


@login_required
@permission_required('ideax.add_category',raise_exception=True)
def category_new(request):
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            category = form.save(commit=False)
            category.author = UserProfile.objects.get(user=request.user)
            category.creation_date = timezone.now()
            category.save()
            messages.success(request, _('Category saved successfully!'))
            audit(request.user.username, get_client_ip(request), 'CREATE_CATEGORY', Category.__name__, category.id)
            return redirect('category_list')
    else:
        form = CategoryForm()
    return render(request, 'ideax/category_new.html', {'form': form})

@login_required
@permission_required('ideax.change_category',raise_exception=True)
def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if request.method == "POST":
        form = CategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, _('Category changed successfully!'))
            audit(request.user.username, get_client_ip(request), 'EDIT_CATEGORY_SAVE', Category.__name__, str(category.id))
            return redirect('category_list')
    else:
        form = CategoryForm(instance=category)

    return render(request, 'ideax/category_edit.html', {'form':form})

@login_required
@permission_required('ideax.delete_category',raise_exception=True)
def category_remove(request, pk):
    category = get_object_or_404(Category, pk=pk)
    category.discarded = True
    category.save()
    messages.success(request, _('Category removed successfully!'))
    audit(request.user.username, get_client_ip(request), 'REMOVE_CATEGORY', Category.__name__, str(pk))
    return redirect('category_list')

@login_required
def category_list(request):
    audit(request.user.username, get_client_ip(request), 'CATEGORY_LIST', Category.__name__, '')
    return render(request, 'ideax/category_list.html', get_category_list())

def get_category_list():
    return {'category_list': Category.objects.filter(discarded=False)}

@login_required
@permission_required('ideax.add_popular_vote',raise_exception=True)
def like_popular_vote(request, pk):
    user = UserProfile.objects.get(user=request.user)
    vote = Popular_Vote.objects.filter(voter=user,idea__pk=pk)

    idea_ = Idea.objects.get(pk=pk)
    like_boolean =  request.path.split("/")[3] == "like"

    if vote.count() == 0:
        like = Popular_Vote(like=like_boolean,voter=user,voting_date=timezone.now(),idea=idea_)
        like.save()
        audit(request.user.username, get_client_ip(request), 'LIKE_SAVE', Popular_Vote.__name__, str(like.id))
    else:
        if vote[0].like == like_boolean:
            audit(request.user.username, get_client_ip(request), 'DISLIKE_SAVE', Popular_Vote.__name__, str(vote[0].id))
            vote.delete()
            like_boolean = None
        else:
            vote.update(like=like_boolean)

    data = dict()
    data['qtde_votes_likes'] = idea_.count_likes()
    data['qtde_votes_dislikes'] = idea_.count_dislikes()
    data['class'] = like_boolean

    return JsonResponse(data)

@login_required
def get_ideas_voted(request, vote):
    user = UserProfile.objects.get(user=request.user)
    ideas_voted = []
    if request.user.is_authenticated:
        ideas_voted = Popular_Vote.objects.filter(like=vote, voter=user).values_list('idea_id',flat=True)

    return ideas_voted

@login_required
def get_ideas_created(request):
    user = UserProfile.objects.get(user=request.user)
    ideas_created = []
    if request.user.is_authenticated:
        ideas_created = Idea.objects.filter(author=user).values_list('id',flat=True)

    return ideas_created

@login_required
@permission_required('ideax.add_phase_history',raise_exception=True)
def change_idea_phase(request, pk, new_phase):
    idea = Idea.objects.get(pk=pk)
    phase = Phase.get_phase_by_id(new_phase)

    if phase != None:
        phase_history_current = Phase_History.objects.get(idea=idea, current=True)
        phase_history_current.current = False
        phase_history_current.save()

        phase_history_new = Phase_History(current_phase=phase.id,
                                          previous_phase=phase_history_current.current_phase,
                                          date_change=timezone.now(),
                                          idea=idea,
                                          author=UserProfile.objects.get(user=request.user),
                                          current=True)
        phase_history_new.save()
        audit(request.user.username, get_client_ip(request), 'CHANGE_PHASE_SAVE', Phase.__name__, str(phase_history_new.id))
        messages.success(request, _('Phase change successfully!'))
        context = {}
        context['idea'] = idea
        try:
            mail_util.send_mail(mail_util.generate_messages("[IdeiaX] - " + str(_('Phase change')), 'ideax/phase_change_email.html', context, [idea.author.user.email]))
        except Exception as e:
            pass


    return redirect('index')

@login_required
def idea_detail(request, pk):
    idea = get_object_or_404(Idea, pk=pk)
    comments = idea.comment_set.filter(deleted=False);

    data = dict()
    data["comments"] = comments
    data["idea"] = idea
    data["idea_id"] = idea.pk
    data["authors"] = idea.authors.all()

    initial = collections.OrderedDict()
    form_ = None
    if idea.score > 0:
        for i in idea.evaluation_set.all().order_by('dimension__id'):
            initial[EvaluationForm.FORMAT_ID % i.dimension.pk] = i
            form_ = EvaluationForm(initial=initial)

    else:
        form_ = EvaluationForm(extra=Dimension.objects.filter(final_date__isnull=True))


    data["form_evaluation"] = None if not form_.fields else form_
    data["evaluation_detail"] = initial

    try:
        data["popular_vote"] = idea.popular_vote_set.get(voter=request.user.userprofile).like
    except Popular_Vote.DoesNotExist:
        pass

    return render(request, 'ideax/idea_detail.html', data)

@login_required
@permission_required('ideax.add_comment',raise_exception=True)
def post_comment(request):
    if not request.user.is_authenticated:
        return JsonResponse({'msg': _("You need to log in to post new comments.")}, status=500)

    raw_comment = request.POST.get('commentContent', None)
    parent_id = request.POST.get('parentId', None)
    author = UserProfile.objects.get(user=request.user)
    idea_id = request.POST.get('ideiaId', None)

    if Profanity_Check.wordcheck().search_badwords(raw_comment):
        return JsonResponse({'msg': _("Please check your message it has inappropriate content.")}, status=500)

    if not raw_comment:
        return JsonResponse({'msg': _("You have to write a comment.")},status=500)

    if not parent_id:
        parent_object = None
    else:
        parent_object = Comment.objects.get(id=parent_id)

    idea = Idea.objects.get(id=idea_id)

    comment = Comment(author=author,
                      raw_comment=mistune.markdown(raw_comment),
                      parent=parent_object,
                      idea=idea,
                      date=timezone.now(),
                      comment_phase=idea.get_current_phase().id,
                      ip=get_ip(request))

    comment.save()
    audit(request.user.username, get_client_ip(request), 'COMMENT_SAVE', Comment.__name__, str(comment.id))
    return JsonResponse({"msg" : _("Your comment has been posted.")})

def idea_comments(request, pk):
    data = dict()
    data['html_list'] = render_to_string('ideax/includes/partial_comments.html',
                                         {"comments" : Comment.objects.filter(idea__id=pk),
                                          "idea_id" : pk})
    return JsonResponse(data)

def get_term_of_user(request):
    term = Use_Term.objects.filter(final_date__gte=datetime.now())
    if term.exists():
        return JsonResponse({"term" : term[0].term })
    else:
        return JsonResponse({"term" : _("No Term of Use found")})

@login_required
def use_term_list(request):
    return render(request, 'ideax/use_term_list.html',get_use_term_list())

def get_use_term_list():
    return {'use_term_list': Use_Term.objects.all(), 'today': date.today()}

@login_required
@permission_required('ideax.add_use_term',raise_exception=True)
def use_term_new(request):
    if request.method == "POST":
        form = UseTermForm(request.POST)
    else:
        form = UseTermForm()
    return save_use_term(request, form, 'ideax/use_term_new.html', True)

@login_required
@permission_required('ideax.change_use_term',raise_exception=True)
def use_term_edit(request, pk):
    use_term = get_object_or_404(Use_Term, pk=pk)
    if request.method == "POST":
        use_term_form = UseTermForm(request.POST, instance=use_term)
    else:
        use_term_form = UseTermForm(instance=use_term)
    return save_use_term(request, use_term_form, 'ideax/use_term_edit.html')

@login_required
def save_use_term(request, form, template_name, new = False):
    if request.method == "POST":
        if form.is_valid():
            active = False
            use_term = form.save(commit=False)
            use_term.creator = UserProfile.objects.get(user=request.user)
            use_terms = Use_Term.objects.all()
            for term in use_terms:
                if term.is_past_due:
                    active = True
                    break
                else:
                    active = False
            if use_term.is_invalid_date():
                messages.error(request, _('Invalid Final Date'))
                return render(request, template_name, {'form': form})
            if active and new:
                messages.error(request, _('Already exists a active Term Of Use'))
                return render(request, template_name, {'form': form})
            use_term.save()
            messages.success(request, _('Term of Use saved successfully!'))
            return redirect('use_term_list')
    else:
        return render(request, template_name, {'form': form})

@login_required
@permission_required('ideax.delete_use_term',raise_exception=True)
def use_term_remove(request, pk):
    use_term = get_object_or_404(Use_Term, pk=pk)
    if request.method == 'GET':
        use_term.final_date = timezone.now()
        use_term.save()
        messages.success(request, _('Terms of Use removed successfully!'))
        return render(request, 'ideax/use_term_list.html', get_use_term_list())

@login_required
def use_term_detail(request, pk):
     use_term = get_object_or_404(Use_Term, pk=pk)
     return render(request, 'ideax/use_term_detail.html', {'use_term' : use_term})

def get_valid_use_term(request):
    use_terms = Use_Term.objects.all()
    for term in use_terms:
        if term.is_past_due:
            valid_use_term = term
            return render(request, 'ideax/use_term.html', {'use_term' : valid_use_term})
    return render(request, 'ideax/use_term.html', {'use_term' : _("No Term of Use found")})

def idea_detail_pdf(request, idea_id):
    idea = Idea.objects.get(pk=idea_id)
    data = dict()
    initial = collections.OrderedDict()
    data['idea'] = idea
    for i in idea.evaluation_set.all().order_by('dimension__id'):
        initial[EvaluationForm.FORMAT_ID % i.dimension.pk] = i

    data['evaluation_detail']= initial
    audit(request.user.username, get_client_ip(request), 'REPORT_GENERATE', Idea.__name__, str(idea.id))
    return render(request, 'ideax/ftec.html', data)
    #pdf_file = generate_pdf_report('ideax/ftec.html', request.build_absolute_uri(), data)
    #response = HttpResponse('pdf_file', content_type='text/plain')
    #response['Content-Disposition'] = 'filename="idea_report.txt"'
    #return response

def get_featured_challenges(request):
    return Challenge.objects.filter(active=True).exclude(discarted=True)

@login_required
def challenge_detail(request, challenge_pk):
     challenge = get_object_or_404(Challenge, pk=challenge_pk)
     return render(request, 'ideax/challenge_detail.html', {'challenge' : challenge, 'ideas': challenge.idea_set.filter(discarded=False)})

@login_required
@permission_required('ideax.add_challenge',raise_exception=True)
def challenge_new(request):
    form = ChallengeForm()

    if request.method == "POST":
        form = ChallengeForm(request.POST, request.FILES)

        if form.is_valid():
            #path  = file_upload(request)
            challenge = form.save(commit=False)
            challenge.author = UserProfile.objects.get(user=request.user)
            challenge.creation_date = timezone.now()
            challenge.save()
            messages.success(request, _('Challenge saved successfully!'))
            return redirect('challenge_list')
    return render(request, 'ideax/challenge_new.html', {'form': form})

@login_required
@permission_required('ideax.change_challenge',raise_exception=True)
def challenge_edit(request, challenge_pk):
    challenge = get_object_or_404(Challenge, pk=challenge_pk)

    if request.method == "POST":
        form = ChallengeForm(request.POST, request.FILES, instance=challenge)

        if form.is_valid():
            challenge = form.save(commit=False)
            challenge.save()
            messages.success(request, _('Challenge saved successfully!'))
            return redirect('challenge_list')
    else:
        form = ChallengeForm(instance=challenge)

    return render(request, 'ideax/challenge_edit.html', {'form': form})

def file_upload(request):
    myfile = request.FILES['image']
    fs = FileSystemStorage()
    filename = fs.save(myfile.name, myfile)
    uploaded_file_url = fs.url(filename)
    return uploaded_file_url

@login_required
@permission_required('ideax.delete_challenge',raise_exception=True)
def challenge_remove(request, pk):
    challenge = get_object_or_404(Challenge, pk=pk)
    if request.method == 'GET':
        challenge.discarted = True
        challenge.save()
        messages.success(request, _('Challenge removed successfully!'))
    else:
        messages.error(request, _('Not supported action'))
    return challenge_list(request)

def challenge_list(request):
    challenges = Challenge.objects.filter(discarted=False)
    return render(request, 'ideax/challenge_list.html', {'challenges': challenges})

@login_required
def report_ideas(request):
    if (request.user.has_perm(settings.PERMISSIONS["MANAGE_IDEA"])):
        csv_path = 'RELATORIO_IDEIAS.csv'
        cursor = connection.cursor()
        cursor.execute('''select i.id as id_ideia ,
    	   i.title as titulo,
    	   i.score as pontuacao,
    	   au.username as usuario,
    	   ic.title as categoria,
    	   sum(case when pv.`like` = 1 then 1 else 0 end) as qtde_likes,
    	   sum(case when pv.`like` = 0 then 1 else 0 end) as qtde_dislikes,
    	   (CASE WHEN ph.current_phase = 1 THEN 'Discussao' WHEN ph.current_phase = 2 THEN 'Avaliacao' WHEN ph.current_phase = 3 THEN 'Ideacao' WHEN ph.current_phase = 4 THEN 'Aprovacao' WHEN ph.current_phase = 5 THEN 'Evolucao' WHEN ph.current_phase = 6 THEN 'Feita' WHEN ph.current_phase = 7 THEN 'Arquivada' WHEN ph.current_phase = 8 THEN 'Pausada' ELSE 0 END) as fase,
    	   (select count(*) from ideax_comment where idea_id = i.id) as qtde_comentarios
            	from ideax_idea i inner join ideax_popular_vote pv on i.id = pv.idea_id
            	inner join ideax_phase_history ph on i.id = ph.idea_id
            	inner join ideax_category ic on i.category_id = ic.id
            	inner join ideax_userprofile iu on i.author_id = iu.id
            	inner join auth_user au on au.id = iu.user_id
            where ph.current = 1 and i.discarded = 0
            group by i.id, ph.current_phase''')
        data = cursor.fetchall()

        with open(csv_path,'w',newline='') as f_handle:
            writer = csv.writer(f_handle)
            header = ['id_ideia', 'titulo', 'pontuacao', 'usuario', 'categoria', 'qtde_likes', 'qtde_dislikes', 'fase', 'qtde_comentarios']
            writer.writerow(header)
            for row in data:
                writer.writerow(row)

        response = StreamingHttpResponse(open(csv_path), content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename=' + csv_path
        return response

    return redirect('index')

@login_required
@permission_required('ideax.add_challenge',raise_exception=True)
def idea_new_from_challenge(request, challenge_pk):
    queryset = get_authors(request.user.email)
    challenge = get_object_or_404(Challenge, pk=challenge_pk)
    form = IdeaForm(initial={'challenge': challenge, 'category': challenge.category},authors=queryset)
    audit(request.user.username, get_client_ip(request), 'CREATE_IDEA_FORM_FROM_MISSION', Idea.__name__, '')
    return save_idea(request, form, 'ideax/idea_new.html', True)

@login_required
@permission_required('ideax.add_category_image',raise_exception=True)
def category_image_new(request):
    if request.method == "POST":
        form = CategoryImageForm(request.POST, request.FILES)

        if form.is_valid():
            category_image = form.save(commit=False)
            category_image.author = UserProfile.objects.get(user=request.user)
            category_image.creation_date = timezone.now()
            category_image.save()
            messages.success(request, _('Category Image saved successfully!'))
            audit(request.user.username, get_client_ip(request), 'CREATE_CATEGORY_IMAGE', Category_Image.__name__, category_image.id)
            return redirect('category_image_list')
    else:
        form = CategoryImageForm()
    return render(request, 'ideax/category_image_new.html', {'form': form})

@login_required
def category_image_list(request):
    category_images = Category_Image.objects.all()
    audit(request.user.username, get_client_ip(request), 'LIST_CATEGORY_IMAGE', Category_Image.__name__,'')
    return render(request, 'ideax/category_image_list.html', {'category_images': category_images})

@login_required
@permission_required('ideax.change_category_image',raise_exception=True)
def category_image_edit(request, pk):
    category_image = get_object_or_404(Category_Image, pk=pk)

    if request.method == "POST":
        form = CategoryImageForm(request.POST, request.FILES, instance=category_image)

        if form.is_valid():
            form.save()
            messages.success(request, _('Category Image changed successfully!'))
            audit(request.user.username, get_client_ip(request), 'EDIT_CATEGORY_IMAGE', Category_Image.__name__, str(pk))
            return redirect('category_image_list')
    else:
        form = CategoryImageForm(instance=category_image)
    return render(request, 'ideax/category_image_edit.html', {'form': form})

@login_required
@permission_required('ideax.delete_category_image',raise_exception=True)
def category_image_remove(request, pk):
    category_image = get_object_or_404(Category_Image, pk=pk)
    category_image.delete()
    messages.success(request, _('Category Image removed successfully!'))
    audit(request.user.username, get_client_ip(request), 'REMOVE_CATEGORY_IMAGE', Category_Image.__name__, str(pk))
    return redirect('category_image_list')

@login_required
def markdown_uploader(request):
    """
    Makdown image upload for locale storage
    and represent as json to markdown editor.
    """
    if request.method == 'POST' and request.is_ajax():
        if 'markdown-image-upload' in request.FILES:
            image = request.FILES['markdown-image-upload']
            image_types = [
                'image/png', 'image/jpg',
                'image/jpeg', 'image/pjpeg', 'image/gif'
            ]
            if image.content_type not in image_types:
                data = json.dumps({
                    'status': 405,
                    'error': _('Bad image format.')
                }, cls=LazyEncoder)
                return HttpResponse(
                    data, content_type='application/json', status=405)

            if image._size > settings.MAX_IMAGE_UPLOAD_SIZE:
                to_MB = settings.MAX_IMAGE_UPLOAD_SIZE / (1024 * 1024)
                data = json.dumps({
                    'status': 405,
                    'error': _('Maximum image file is %(size) MB.') % {'size': to_MB}
                }, cls=LazyEncoder)
                return HttpResponse(
                    data, content_type='application/json', status=405)

            img_uuid = "{0}-{1}".format(uuid.uuid4().hex[:10], image.name.replace(' ', '-'))
            tmp_file = os.path.join(settings.MARTOR_UPLOAD_PATH, img_uuid)
            def_path = default_storage.save(tmp_file, ContentFile(image.read()))
            img_url = os.path.join(settings.MEDIA_URL, def_path)

            data = json.dumps({
                'status': 200,
                'link': img_url,
                'name': image.name
            })
            return HttpResponse(data, content_type='application/json')
        return HttpResponse(_('Invalid request!'))
    return HttpResponse(_('Invalid request!'))

def idea_search(request):
    return idea_filter(request, search_part=request.POST.get('seach_filter', None))

def user_profile_page(request):
    return render(request, 'ideax/user_profile.html')

def get_authors(removed_author):
    return UserProfile.objects.filter(user__is_staff=False).exclude(user__email__isnull=True).exclude(user__email=removed_author)